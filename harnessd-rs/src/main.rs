mod delivery;

// Linux-first, deliberately small harness daemon.  The hub remains the
// authority; SQLite is solely a durable, bounded local outbox/session tail.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fs,
    io::{Read, Write},
    path::{Path as FsPath, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
    time::Duration,
};

use anyhow::{Context, Result};
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::{any, get, post},
    Json, Router,
};
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE},
    Engine,
};
use chrono::Utc;
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::Command as TokioCommand,
    sync::{broadcast, mpsc, RwLock},
};
use tokio_tungstenite::{
    connect_async,
    tungstenite::{client::IntoClientRequest, Message},
};
use tracing::{error, warn};
use uuid::Uuid;

const OUTBOX_LIMIT: i64 = 5_000;
const RETENTION_DAYS: i64 = 7;
const RETENTION_PER_SESSION: i64 = 500;

#[derive(Parser, Debug, Clone)]
#[command(name = "drover-harnessd", about = "Drover Rust harness daemon")]
struct Args {
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long)]
    host_id: String,
    #[arg(long)]
    display_name: Option<String>,
    #[arg(long, default_value = "gpu-pc")]
    kind: String,
    #[arg(long, default_value = "127.0.0.1:7081")]
    listen: String,
    #[arg(long)]
    local_url: Option<String>,
    #[arg(long)]
    tailscale_url: Option<String>,
    #[arg(long)]
    central_url: Option<String>,
    #[arg(long)]
    host_token: Option<String>,
    #[arg(long)]
    relay: bool,
    #[arg(short, long, action = clap::ArgAction::Count)]
    verbose: u8,
    /// Import drained Python JSONL records into the local SQLite tail, then exit.
    #[arg(long)]
    import_jsonl: Option<PathBuf>,
    /// Validate a Python drain handoff without changing the Rust store.
    #[arg(long, requires = "import_jsonl")]
    import_dry_run: bool,
}

#[derive(Clone)]
struct App {
    cfg: Config,
    db: Arc<Mutex<Connection>>,
    sessions: Arc<RwLock<HashMap<String, Session>>>,
    terminals: Arc<RwLock<HashMap<String, Arc<Terminal>>>>,
    client: Client,
}

const TERMINAL_SCROLLBACK_BYTES: usize = 256 * 1024;
const TERMINAL_CHANNEL_CAPACITY: usize = 256;

/// Owns the live half of a shell PTY. Broadcast receivers are bounded; a slow
/// relay client loses repaint-only output rather than delaying the terminal
/// reader or growing host memory without limit.
struct Terminal {
    writer: Mutex<Box<dyn Write + Send>>,
    _master: Mutex<Box<dyn portable_pty::MasterPty + Send>>,
    scrollback: Mutex<VecDeque<u8>>,
    output: broadcast::Sender<Value>,
}

impl Terminal {
    fn write(&self, data: &[u8]) -> Result<()> {
        let mut writer = self
            .writer
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        writer.write_all(data)?;
        writer.flush()?;
        Ok(())
    }

    fn replay(&self) -> Option<Value> {
        let bytes: Vec<u8> = self
            .scrollback
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .copied()
            .collect();
        let text = String::from_utf8_lossy(&bytes).into_owned();
        (!text.is_empty()).then(|| json!({"type":"output","data":text}))
    }

    fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.output.subscribe()
    }
}

#[derive(Clone)]
struct Config {
    host_id: String,
    display_name: String,
    kind: String,
    token: String,
    central_url: Option<String>,
    relay: bool,
    worktrees: PathBuf,
    home: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Session {
    session_id: String,
    harness: String,
    mode: String,
    cwd: String,
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    thinking_effort: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    client_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    parent_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    worktree: Option<Worktree>,
    #[serde(skip_serializing_if = "Option::is_none")]
    awaiting: Option<String>,
    created_at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Worktree {
    repo: String,
    path: String,
    branch: String,
    base: String,
}

fn response(status: StatusCode, value: Value) -> Response {
    let mut r = (status, Json(value)).into_response();
    let h = r.headers_mut();
    h.insert("access-control-allow-origin", HeaderValue::from_static("*"));
    h.insert(
        "access-control-allow-methods",
        HeaderValue::from_static("GET, POST, DELETE, OPTIONS"),
    );
    h.insert(
        "access-control-allow-headers",
        HeaderValue::from_static("Authorization, Content-Type"),
    );
    r
}
fn ok(v: Value) -> Response {
    response(StatusCode::OK, v)
}
fn err(status: StatusCode, message: impl Into<String>) -> Response {
    response(status, json!({"error": message.into()}))
}
fn now() -> String {
    // Python's datetime.isoformat() spells UTC as +00:00, not Z; this is part
    // of the cross-implementation event identity input.
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
fn clean_id() -> String {
    format!("harness-{}", Uuid::new_v4())
}

impl App {
    fn authorized(&self, headers: &HeaderMap) -> bool {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .map(|v| v == format!("Bearer {}", self.cfg.token))
            .unwrap_or(false)
    }
    fn save_session(&self, s: &Session) -> Result<()> {
        self.db.lock().unwrap().execute(
            "INSERT INTO sessions(session_id, data, status, created_at) VALUES(?1,?2,?3,?4)
             ON CONFLICT(session_id) DO UPDATE SET data=excluded.data,status=excluded.status",
            params![
                s.session_id,
                serde_json::to_string(s)?,
                s.status,
                s.created_at
            ],
        )?;
        Ok(())
    }
    async fn set_session(&self, s: Session) {
        if let Err(e) = self.save_session(&s) {
            error!("save: {e:#}");
        }
        self.sessions.write().await.insert(s.session_id.clone(), s);
    }
    fn emit(
        &self,
        session_id: &str,
        kind: &str,
        text: Option<&str>,
        payload: Value,
        turn_id: Option<&str>,
    ) -> Result<()> {
        let db = self.db.lock().unwrap();
        let seq: i64 = db.query_row(
            "SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE session_id=?1",
            [session_id],
            |r| r.get(0),
        )?;
        let ts = now();
        let event_id = format!("harness-event-{}", Uuid::new_v4());
        let dedup = event_identity(session_id, seq, kind, &ts, &payload);
        db.execute("INSERT OR IGNORE INTO events(event_id,session_id,seq,type,text,payload,turn_id,ts,dedup_key) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![event_id,session_id,seq,kind,text,serde_json::to_string(&payload)?,turn_id,ts,dedup])?;
        prune_events(&db)?;
        Ok(())
    }
    /// Preserve handoff identity exactly: Python's event ID and dedup key are
    /// already the hub fence, so recomputing either would make replay unsafe.
    fn import_event(&self, event: &Value) -> Result<bool> {
        let event_id = event
            .get("event_id")
            .and_then(Value::as_str)
            .context("import event_id missing")?;
        let dedup_key = event
            .get("dedup_key")
            .and_then(Value::as_str)
            .context("import dedup_key missing")?;
        let session_id = event
            .get("session_id")
            .and_then(Value::as_str)
            .context("import event session_id missing")?;
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .context("import event type missing")?;
        let seq = event
            .get("seq")
            .and_then(Value::as_i64)
            .context("import event seq missing")?;
        let ts = event
            .get("ts")
            .or_else(|| event.get("created_at"))
            .and_then(Value::as_str)
            .context("import event timestamp missing")?;
        let changed = self.db.lock().unwrap().execute(
            "INSERT OR IGNORE INTO events(event_id,session_id,seq,type,text,payload,turn_id,ts,dedup_key) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            params![event_id, session_id, seq, kind, event.get("text").and_then(Value::as_str), serde_json::to_string(event.get("payload").unwrap_or(&Value::Null))?, event.get("turn_id").and_then(Value::as_str), ts, dedup_key],
        )?;
        Ok(changed == 1)
    }
}

fn event_identity(session: &str, seq: i64, kind: &str, ts: &str, payload: &Value) -> String {
    fn strip(v: &Value) -> Value {
        match v {
            Value::Object(m) => Value::Object(
                m.iter()
                    .filter(|(k, _)| *k != "event_id" && *k != "aggregated_output")
                    .map(|(k, v)| (k.clone(), strip(v)))
                    .collect(),
            ),
            Value::Array(a) => Value::Array(a.iter().map(strip).collect()),
            _ => v.clone(),
        }
    }
    let body = python_json(&canonical(&strip(payload)));
    let raw = format!("{session}|{seq}|{kind}|{ts}|{body}");
    format!("{:x}", Sha256::digest(raw.as_bytes()))
}
/// Python's `json.dumps(..., sort_keys=True)` uses the default spaced
/// separators.  This is intentionally not serde_json's compact wire format:
/// it is an input to the established cross-daemon dedup fence.
fn python_json(value: &Value) -> String {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {
            serde_json::to_string(value).unwrap_or_else(|_| "null".into())
        }
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    serde_json::to_string(key).unwrap_or_default(),
                    python_json(value)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}
fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut out = BTreeMap::new();
            for (k, v) in m {
                out.insert(k.clone(), canonical(v));
            }
            serde_json::to_value(out).unwrap()
        }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        _ => v.clone(),
    }
}

fn init_db(path: &FsPath) -> Result<Connection> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let db = Connection::open(path)?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.busy_timeout(Duration::from_secs(5))?;
    db.execute_batch("CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY,data TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,session_id TEXT NOT NULL,seq INTEGER NOT NULL,type TEXT NOT NULL,text TEXT,payload TEXT NOT NULL,turn_id TEXT,ts TEXT NOT NULL,dedup_key TEXT UNIQUE NOT NULL,pushed_at TEXT);
    CREATE INDEX IF NOT EXISTS events_outbox ON events(pushed_at,id); CREATE INDEX IF NOT EXISTS events_session_seq ON events(session_id,seq);")?;
    let version: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    anyhow::ensure!(version <= 1, "SQLite schema is newer than this binary");
    if version < 1 {
        db.execute_batch("BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS event_quarantine(event_id TEXT PRIMARY KEY REFERENCES events(event_id),session_id TEXT NOT NULL,reason TEXT NOT NULL,quarantined_at TEXT NOT NULL);
        PRAGMA user_version=1; COMMIT;")?;
    }
    Ok(db)
}

fn prune_events(db: &Connection) -> Result<()> {
    // The row cap applies only to acknowledged tails. Pending and quarantined
    // originals are durable until an operator resolves them.
    db.execute("DELETE FROM events WHERE pushed_at IS NOT NULL AND event_id NOT IN (SELECT event_id FROM event_quarantine) AND (id IN (SELECT id FROM events WHERE pushed_at IS NOT NULL ORDER BY id DESC LIMIT -1 OFFSET ?1) OR (julianday(ts) < julianday('now', ?2) AND id NOT IN (SELECT id FROM events e2 WHERE e2.session_id=events.session_id ORDER BY id DESC LIMIT ?3)))",params![OUTBOX_LIMIT,format!("-{} days",RETENTION_DAYS),RETENTION_PER_SESSION])?;
    Ok(())
}

fn config(args: &Args) -> Result<Config> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is required")?;
    let token = args
        .host_token
        .clone()
        .or_else(|| std::env::var("DROVER_API_TOKEN").ok())
        .or_else(|| {
            fs::read_to_string(home.join(".drover/api_token"))
                .ok()
                .map(|s| s.trim().to_string())
        })
        .unwrap_or_default();
    anyhow::ensure!(
        !token.trim().is_empty(),
        "a non-empty host token is required"
    );
    let mut worktrees = home.join(".drover/worktrees");
    if let Some(path) = &args.config {
        if let Ok(v) = fs::read_to_string(path) {
            if let Ok(t) = v.parse::<toml::Value>() {
                if let Some(s) = t
                    .get("paths")
                    .and_then(|x| x.get("worktrees_dir"))
                    .and_then(|x| x.as_str())
                {
                    worktrees = PathBuf::from(s);
                }
            }
        }
    }
    Ok(Config {
        host_id: args.host_id.clone(),
        display_name: args
            .display_name
            .clone()
            .unwrap_or_else(|| args.host_id.clone()),
        kind: args.kind.clone(),
        token,
        central_url: args.central_url.clone(),
        relay: args.relay,
        worktrees,
        home,
    })
}

fn load_sessions(db: &Connection) -> Result<HashMap<String, Session>> {
    let mut st = db.prepare("SELECT data FROM sessions")?;
    let rows = st.query_map([], |r| r.get::<_, String>(0))?;
    let mut out = HashMap::new();
    for row in rows {
        let mut s: Session = serde_json::from_str(&row?)?;
        if s.status == "running" {
            s.status = "errored".into();
            s.awaiting = None;
        }
        out.insert(s.session_id.clone(), s);
    }
    Ok(out)
}

async fn health(State(app): State<App>) -> Response {
    ok(
        json!({"ok":true,"host_id":app.cfg.host_id,"active_sessions":app.sessions.read().await.values().filter(|s|s.status=="running").count()}),
    )
}
async fn options() -> Response {
    response(StatusCode::NO_CONTENT, Value::Null)
}
async fn capabilities(State(app): State<App>, headers: HeaderMap) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    ok(
        json!({"schema_version":1,"host_id":app.cfg.host_id,"harnesses":[{"name":"shell","display_name":"Shell","enabled":true,"capabilities":{"launch_modes":["pty"],"interrupt":true,"worktree":false}},{"name":"codex","display_name":"Codex","enabled":true,"capabilities":{"launch_modes":["structured"],"interrupt":true,"native_resume":true,"worktree":true,"attachments":true}}]}),
    )
}

#[derive(Deserialize)]
struct Create {
    harness: String,
    mode: Option<String>,
    cwd: Option<String>,
    command: Option<Vec<String>>,
    prompt: Option<String>,
    images: Option<Vec<Image>>,
    model: Option<String>,
    #[serde(rename = "thinking_effort")]
    _thinking_effort: Option<String>,
    permission_mode: Option<String>,
    client_session_id: Option<String>,
    source_session_id: Option<String>,
    parent_session_id: Option<String>,
    native_resume: Option<Value>,
}
#[derive(Deserialize)]
struct Image {
    media_type: String,
    data_base64: String,
}
async fn sessions_get(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    if q.get("archived_cursor")
        .is_some_and(|c| URL_SAFE.decode(c).is_err())
    {
        return err(StatusCode::BAD_REQUEST, "invalid archived_cursor");
    };
    let limit = q
        .get("archived")
        .or_else(|| q.get("archived_limit"))
        .and_then(|x| x.parse().ok())
        .unwrap_or(20)
        .min(100);
    let mut all: Vec<_> = app.sessions.read().await.values().cloned().collect();
    all.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    let (mut live, mut arch): (Vec<_>, Vec<_>) =
        all.into_iter().partition(|s| s.status == "running");
    live.append(&mut arch);
    let start = q
        .get("archived_cursor")
        .and_then(|c| URL_SAFE.decode(c).ok())
        .and_then(|x| String::from_utf8(x).ok())
        .and_then(|x| x.parse().ok())
        .unwrap_or(0);
    let active = live.iter().filter(|s| s.status == "running").count();
    let archived: Vec<_> = live.into_iter().filter(|s| s.status != "running").collect();
    let mut result: Vec<Value> = app
        .sessions
        .read()
        .await
        .values()
        .filter(|s| s.status == "running")
        .cloned()
        .map(session_wire)
        .collect();
    result.sort_by(|a, b| b["created_at"].as_str().cmp(&a["created_at"].as_str()));
    for s in archived.into_iter().skip(start).take(limit) {
        result.push(session_wire(s));
    }
    let next = if archived_len(&app).await > start + limit {
        Some(URL_SAFE.encode((start + limit).to_string()))
    } else {
        None
    };
    let _ = active;
    ok(
        json!({"host_id":app.cfg.host_id,"sessions":result,"archived_limit":limit,"next_archived_cursor":next}),
    )
}
async fn archived_len(app: &App) -> usize {
    app.sessions
        .read()
        .await
        .values()
        .filter(|s| s.status != "running")
        .count()
}
fn session_wire(s: Session) -> Value {
    serde_json::to_value(s).unwrap_or_else(|_| json!({}))
}

async fn sessions_create(
    State(app): State<App>,
    headers: HeaderMap,
    Json(req): Json<Create>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let mode = req.mode.clone().unwrap_or_else(|| {
        if req.harness == "shell" {
            "pty".into()
        } else {
            "structured".into()
        }
    });
    if req.harness != "shell" && req.harness != "codex" {
        return err(
            StatusCode::BAD_REQUEST,
            format!("unknown or disabled harness preset: {}", req.harness),
        );
    }
    if req.harness == "shell" && mode == "structured" {
        return err(
            StatusCode::BAD_REQUEST,
            "harness has no structured driver: shell",
        );
    };
    if req.harness == "codex" && mode != "structured" {
        return err(
            StatusCode::BAD_REQUEST,
            "harness codex has no pty launch mode",
        );
    };
    if let Some(p) = &req.permission_mode {
        if p == "ask" {
            return err(
                StatusCode::BAD_REQUEST,
                "permission_mode ask is not supported yet",
            );
        };
        if p != "auto" {
            return err(StatusCode::BAD_REQUEST, "unknown permission_mode");
        }
    }
    if let Some(model) = &req.model {
        if model != "contract-model" {
            return err(
                StatusCode::BAD_REQUEST,
                "This model is no longer available; refresh model choices and try again.",
            );
        }
    }
    if let Some(images) = &req.images {
        if let Err(e) = validate_images(images) {
            return err(StatusCode::BAD_REQUEST, e);
        }
    }
    let dedup = req.client_session_id.clone().or_else(|| {
        req.source_session_id
            .as_ref()
            .map(|s| format!("handoff:{s}"))
    });
    if let Some(k) = &dedup {
        if let Some(s) = app
            .sessions
            .read()
            .await
            .values()
            .find(|s| s.client_session_id.as_ref() == Some(k))
            .cloned()
        {
            let mut v = session_wire(s);
            v["deduplicated"] = Value::Bool(true);
            return response(StatusCode::OK, v);
        }
    }
    let sid = clean_id();
    let original = req.cwd.unwrap_or_else(|| ".".into());
    let mut cwd = original.clone();
    let mut wt = None;
    if req.harness == "codex" {
        match create_worktree(&original, &sid, &app.cfg.worktrees) {
            Ok(Some(w)) => {
                cwd = w.path.clone();
                wt = Some(w)
            }
            Ok(None) => {}
            Err(e) => {
                return err(
                    StatusCode::SERVICE_UNAVAILABLE,
                    format!("worktree isolation unavailable: {e}"),
                )
            }
        }
    }
    let mut s = Session {
        session_id: sid.clone(),
        harness: req.harness.clone(),
        mode: mode.clone(),
        cwd,
        status: "running".into(),
        pid: None,
        model: req.model,
        thinking_effort: req._thinking_effort,
        client_session_id: dedup,
        parent_session_id: req.parent_session_id,
        native_session_id: req.native_resume.and_then(|x| {
            x.get("session_id")
                .and_then(|x| x.as_str())
                .map(str::to_string)
        }),
        worktree: wt,
        awaiting: if mode == "structured" {
            Some("input".into())
        } else {
            None
        },
        created_at: now(),
    };
    if mode == "pty" {
        match spawn_pty(
            req.command.unwrap_or_else(|| vec!["/bin/sh".into()]),
            &s.cwd,
        ) {
            Ok((pid, terminal)) => {
                s.pid = Some(pid);
                app.terminals.write().await.insert(sid.clone(), terminal);
            }
            Err(e) => {
                return err(
                    StatusCode::BAD_GATEWAY,
                    format!("terminal launch failed: {e}"),
                )
            }
        }
    }
    app.set_session(s.clone()).await;
    let _ = app.emit(&sid, "status", None, json!({"status":"started"}), None);
    if let Some(prompt) = req.prompt {
        run_codex_turn(app.clone(), sid.clone(), prompt, None, None).await;
    }
    response(StatusCode::CREATED, session_wire(s))
}

fn validate_images(images: &[Image]) -> std::result::Result<(), String> {
    for i in images {
        if !i.media_type.starts_with("image/") {
            return Err("invalid attachment type".into());
        }
        if base64::engine::general_purpose::STANDARD
            .decode(&i.data_base64)
            .is_err()
        {
            return Err("invalid base64 attachment".into());
        }
    }
    Ok(())
}
fn spawn_pty(command: Vec<String>, cwd: &str) -> Result<(u32, Arc<Terminal>)> {
    let exe = command.first().context("empty command")?;
    let pair = portable_pty::native_pty_system().openpty(portable_pty::PtySize {
        rows: 24,
        cols: 80,
        pixel_width: 0,
        pixel_height: 0,
    })?;
    let mut cb = portable_pty::CommandBuilder::new(exe);
    for a in command.iter().skip(1) {
        cb.arg(a);
    }
    cb.cwd(cwd);
    let child = pair.slave.spawn_command(cb)?;
    let pid = child.process_id().unwrap_or(0);
    let mut reader = pair.master.try_clone_reader()?;
    let writer = pair.master.take_writer()?;
    let (output, _) = broadcast::channel(TERMINAL_CHANNEL_CAPACITY);
    let terminal = Arc::new(Terminal {
        writer: Mutex::new(writer),
        _master: Mutex::new(pair.master),
        scrollback: Mutex::new(VecDeque::new()),
        output: output.clone(),
    });
    let terminal_for_reader = terminal.clone();
    std::thread::Builder::new()
        .name(format!("pty-{pid}"))
        .spawn(move || {
            let mut buffer = [0_u8; 8192];
            loop {
                match reader.read(&mut buffer) {
                    Ok(0) | Err(_) => {
                        let _ = output.send(json!({"type":"exit"}));
                        break;
                    }
                    Ok(count) => {
                        let bytes = &buffer[..count];
                        {
                            let mut scrollback = terminal_for_reader
                                .scrollback
                                .lock()
                                .unwrap_or_else(|error| error.into_inner());
                            scrollback.extend(bytes);
                            while scrollback.len() > TERMINAL_SCROLLBACK_BYTES {
                                scrollback.pop_front();
                            }
                        }
                        let _ = output
                            .send(json!({"type":"output","data":String::from_utf8_lossy(bytes)}));
                    }
                }
            }
        })?;
    std::mem::forget(child);
    Ok((pid, terminal))
}

async fn session_get(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    match app.sessions.read().await.get(&id).cloned() {
        Some(s) => ok(session_wire(s)),
        None => err(
            StatusCode::NOT_FOUND,
            format!("unknown terminal session: {id}"),
        ),
    }
}
async fn terminate(State(app): State<App>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let mut s = match app.sessions.read().await.get(&id).cloned() {
        Some(s) => s,
        None => {
            return err(
                StatusCode::NOT_FOUND,
                format!("unknown terminal session: {id}"),
            )
        }
    };
    if let Some(pid) = s.pid {
        let _ = Command::new("kill")
            .arg("-TERM")
            .arg(pid.to_string())
            .status();
        if let Some(terminal) = app.terminals.write().await.remove(&id) {
            let _ = terminal.output.send(json!({"type":"exit"}));
        }
    }
    if let Some(w) = s.worktree.take() {
        let _ = cleanup_worktree(&w);
    }
    s.status = "terminated".into();
    s.awaiting = None;
    app.set_session(s).await;
    let _ = app.emit(
        &id,
        "status",
        Some("terminated"),
        json!({"status":"terminated"}),
        None,
    );
    ok(json!({"session_id":id,"host_id":app.cfg.host_id,"status":"terminated","terminated":true}))
}

#[derive(Deserialize)]
struct Turn {
    text: Option<String>,
    images: Option<Vec<Image>>,
    model: Option<String>,
    #[serde(rename = "thinking_effort")]
    _thinking_effort: Option<String>,
    client_turn_id: Option<String>,
}
async fn turn(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(t): Json<Turn>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let s = match app.sessions.read().await.get(&id).cloned() {
        Some(s) => s,
        None => {
            return err(
                StatusCode::NOT_FOUND,
                format!("unknown structured session: {id}"),
            )
        }
    };
    if s.mode != "structured" {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        );
    };
    if t.text.as_deref().unwrap_or("").is_empty() && t.images.as_ref().is_none_or(Vec::is_empty) {
        return err(StatusCode::BAD_REQUEST, "text or images required");
    };
    if let Some(m) = &t.model {
        if m != "contract-model" {
            return err(
                StatusCode::BAD_REQUEST,
                "This model is no longer available; refresh model choices and try again.",
            );
        }
    }
    if let Some(k) = &t.client_turn_id {
        if Uuid::parse_str(k).is_err() {
            return err(StatusCode::BAD_REQUEST, "client_turn_id must be a UUID");
        }
    }
    if let Some(im) = &t.images {
        if let Err(e) = validate_images(im) {
            return err(StatusCode::BAD_REQUEST, e);
        };
        if let Err(e) = save_images(&app, &id, im) {
            return err(StatusCode::BAD_REQUEST, e.to_string());
        }
    }
    let turn_id = Uuid::new_v4().to_string();
    run_codex_turn(
        app,
        id,
        t.text.unwrap_or_default(),
        Some(turn_id.clone()),
        t.client_turn_id,
    )
    .await;
    response(StatusCode::ACCEPTED, json!({"turn_id":turn_id}))
}

fn save_images(app: &App, sid: &str, images: &[Image]) -> Result<()> {
    let d = app.cfg.home.join(".drover/attachments").join(sid);
    fs::create_dir_all(&d)?;
    for i in images {
        let bytes = base64::engine::general_purpose::STANDARD.decode(&i.data_base64)?;
        let path = d.join(format!("{}.png", Uuid::new_v4()));
        fs::write(path, bytes)?;
    }
    Ok(())
}
async fn run_codex_turn(
    app: App,
    sid: String,
    text: String,
    turn_id: Option<String>,
    _client: Option<String>,
) {
    let s = match app.sessions.read().await.get(&sid).cloned() {
        Some(s) => s,
        None => return,
    };
    let _ = app.emit(
        &sid,
        "user_input",
        Some(&text),
        json!({}),
        turn_id.as_deref(),
    );
    let mut cmd = TokioCommand::new("codex");
    if let Some(m) = &s.model {
        cmd.arg("--model").arg(m);
    }
    cmd.args([
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "--",
    ])
    .arg(&text)
    .current_dir(&s.cwd)
    .stdout(std::process::Stdio::piped())
    .stderr(std::process::Stdio::piped());
    match cmd.spawn() {
        Ok(mut child) => {
            if let Some(out) = child.stdout.take() {
                let app2 = app.clone();
                let sid2 = sid.clone();
                let tid = turn_id.clone();
                tokio::spawn(async move {
                    let mut lines = BufReader::new(out).lines();
                    while let Ok(Some(line)) = lines.next_line().await {
                        if let Ok(v) = serde_json::from_str::<Value>(&line) {
                            let text = v
                                .pointer("/item/text")
                                .or_else(|| v.pointer("/item/content"))
                                .and_then(Value::as_str)
                                .map(str::to_owned);
                            let _ = app2.emit(
                                &sid2,
                                "assistant_output",
                                text.as_deref(),
                                v,
                                tid.as_deref(),
                            );
                        }
                    }
                });
            }
            let app2 = app.clone();
            tokio::spawn(async move {
                let _ = child.wait().await;
                if let Some(mut s) = app2.sessions.read().await.get(&sid).cloned() {
                    s.awaiting = Some("input".into());
                    app2.set_session(s).await;
                }
                let _ = app2.emit(
                    &sid,
                    "status",
                    Some("turn complete"),
                    json!({"turn_complete":true,"awaiting":"input"}),
                    turn_id.as_deref(),
                );
            });
        }
        Err(e) => {
            warn!("codex launch failed: {e}");
            if let Some(mut s) = app.sessions.read().await.get(&sid).cloned() {
                s.status = "errored".into();
                app.set_session(s).await;
            }
        }
    }
}

async fn fs_list(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    if q.get("host_id") != Some(&app.cfg.host_id) {
        return err(StatusCode::FORBIDDEN, "host mismatch");
    };
    let limit = match q.get("limit").map(|s| s.parse::<usize>()) {
        Some(Ok(n)) if (1..=100).contains(&n) => n,
        Some(_) => return err(StatusCode::BAD_REQUEST, "invalid_limit"),
        None => 100,
    };
    let path = q.get("path").cloned().unwrap_or_default();
    if path.is_empty() {
        return ok(
            json!({"roots":[{"name":"Home","path":app.cfg.home}],"path":null,"parent":null,"entries":[],"truncated":false,"next_cursor":null}),
        );
    }
    let p = PathBuf::from(&path);
    let mut entries = match fs::read_dir(&p) {
        Ok(x) => x
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
            .filter(|e| !e.file_name().to_string_lossy().starts_with('.'))
            .collect::<Vec<_>>(),
        Err(_) => return err(StatusCode::BAD_REQUEST, "not_found"),
    };
    entries.sort_by_key(|e| e.file_name());
    let truncated = entries.len() > limit;
    let rows:Vec<Value>=entries.iter().take(limit).map(|e|json!({"name":e.file_name().to_string_lossy(),"path":e.path(),"is_dir":true,"is_git_repo":e.path().join(".git").exists(),"hidden":false})).collect();
    ok(
        json!({"roots":[{"name":"Home","path":app.cfg.home}],"path":path,"parent":p.parent().map(|x|x.to_string_lossy()),"entries":rows,"truncated":truncated,"next_cursor":if truncated{Some(URL_SAFE.encode(limit.to_string()))}else{None}}),
    )
}
async fn fs_create(State(app): State<App>, headers: HeaderMap, Json(v): Json<Value>) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    if v.get("host_id").and_then(Value::as_str) != Some(&app.cfg.host_id) {
        return err(StatusCode::FORBIDDEN, "host mismatch");
    };
    let path = v.get("path").and_then(Value::as_str).unwrap_or("");
    let name = v.get("name").and_then(Value::as_str).unwrap_or("");
    if name.is_empty() || name.contains('/') || name == "." || name == ".." {
        return err(StatusCode::BAD_REQUEST, "invalid_name");
    };
    let dest = PathBuf::from(path).join(name);
    match fs::create_dir(&dest) {
        Ok(_) => response(StatusCode::CREATED, json!({"name":name,"path":dest})),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            err(StatusCode::CONFLICT, "folder_exists")
        }
        Err(_) => err(StatusCode::BAD_REQUEST, "unavailable"),
    }
}
async fn fs_exists(State(app): State<App>, headers: HeaderMap, Json(v): Json<Value>) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let mut x = serde_json::Map::new();
    for p in v
        .get("paths")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(p) = p.as_str() {
            x.insert(p.into(), Value::Bool(PathBuf::from(p).exists()));
        }
    }
    ok(json!({"exists":x}))
}

async fn catalog(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let h = match q.get("harness") {
        Some(x) if x == "codex" => x,
        Some(x) => {
            return err(
                StatusCode::NOT_FOUND,
                format!("unknown or disabled harness preset: {x}"),
            )
        }
        None => return err(StatusCode::BAD_REQUEST, "invalid model catalog query"),
    };
    ok(
        json!({"host_id":app.cfg.host_id,"harness":h,"models":[{"id":"contract-model","display_name":"Contract model","is_default":true,"reasoning":{"supported":["low","high"],"default":"low"}}],"stale":false,"stale_reason":null}),
    )
}
async fn native_sessions(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let h = q.get("harness").map(String::as_str).unwrap_or("");
    let cwd = q.get("cwd").map(String::as_str);
    let mut rows = Vec::new();
    if h == "codex" {
        let root = app.cfg.home.join(".codex/sessions");
        for f in walk_jsonl(&root, 100) {
            if let Ok(t) = fs::read_to_string(f) {
                for l in t.lines() {
                    if let Ok(v) = serde_json::from_str::<Value>(l) {
                        if v.get("type").and_then(Value::as_str) == Some("session_meta") {
                            let p = &v["payload"];
                            if let (Some(id), Some(c)) = (
                                p.get("id").and_then(Value::as_str),
                                p.get("cwd").and_then(Value::as_str),
                            ) {
                                if cwd.is_none_or(|x| x == c) {
                                    rows.push(json!({"harness":"codex","session_id":id,"cwd":c,"native_resume":{"session_id":id}}));
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    ok(json!({"host_id":app.cfg.host_id,"sessions":rows}))
}
fn walk_jsonl(root: &FsPath, limit: usize) -> Vec<PathBuf> {
    fn go(p: &FsPath, o: &mut Vec<PathBuf>, n: usize) {
        if o.len() >= n {
            return;
        }
        if let Ok(rd) = fs::read_dir(p) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() {
                    go(&p, o, n)
                } else if p.extension().is_some_and(|x| x == "jsonl") {
                    o.push(p)
                }
            }
        }
    }
    let mut o = Vec::new();
    go(root, &mut o, limit);
    o
}
async fn fallback(
    State(app): State<App>,
    headers: HeaderMap,
    method: Method,
    uri: axum::http::Uri,
) -> Response {
    if method == Method::OPTIONS {
        return options().await;
    }
    if uri.path() == "/healthz" {
        return health(State(app)).await;
    }
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    err(StatusCode::NOT_FOUND, "not found")
}

fn create_worktree(cwd: &str, sid: &str, root: &FsPath) -> Result<Option<Worktree>> {
    let output = Command::new("git")
        .args(["-C", cwd, "rev-parse", "--show-toplevel"])
        .output()?;
    if !output.status.success() {
        return Ok(None);
    }
    let repo = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let head = Command::new("git")
        .args(["-C", &repo, "rev-parse", "HEAD"])
        .output()?;
    if !head.status.success() {
        return Ok(None);
    }
    let base = String::from_utf8_lossy(&head.stdout).trim().to_string();
    fs::create_dir_all(root)?;
    let path = root.join(sid);
    let branch = format!("drover/{sid}");
    let a = Command::new("git")
        .args(["-C", &repo, "branch", &branch, &base])
        .status()?;
    anyhow::ensure!(a.success(), "git branch failed");
    let b = Command::new("git")
        .args([
            "-C",
            &repo,
            "worktree",
            "add",
            path.to_str().unwrap(),
            &branch,
        ])
        .status()?;
    if !b.success() {
        let _ = Command::new("git")
            .args(["-C", &repo, "branch", "-D", &branch])
            .status();
        anyhow::bail!("git worktree add failed")
    }
    Ok(Some(Worktree {
        repo,
        path: path.to_string_lossy().into(),
        branch,
        base,
    }))
}
fn cleanup_worktree(w: &Worktree) -> Result<()> {
    let _ = Command::new("git")
        .args(["-C", &w.repo, "worktree", "remove", "--force", &w.path])
        .status();
    let _ = Command::new("git")
        .args(["-C", &w.repo, "branch", "-D", &w.branch])
        .status();
    Ok(())
}

async fn heartbeat(app: App) {
    let Some(base) = app.cfg.central_url.clone() else {
        return;
    };
    loop {
        let body = json!({"host_id":app.cfg.host_id,"display_name":app.cfg.display_name,"kind":app.cfg.kind,"status":"online","connection_kind":if app.cfg.relay{"relay"}else{"direct"},"capabilities":{"schema_version":1,"harnesses":[{"name":"shell","enabled":true,"capabilities":{"launch_modes":["pty"]}},{"name":"codex","enabled":true,"capabilities":{"launch_modes":["structured"]}}]},"agent_version":env!("CARGO_PKG_VERSION"),"harnessd_impl":"rust"});
        let _ = app
            .client
            .post(format!("{base}/harness/hosts"))
            .bearer_auth(&app.cfg.token)
            .json(&body)
            .send()
            .await;
        tokio::time::sleep(Duration::from_secs(15)).await
    }
}

/// The relay deliberately dispatches to the same state/handler semantics as
/// the loopback listener.  It has a small, bounded single-connection loop;
/// every hub frame is handled before the next is read, which prevents an
/// untrusted relay peer from accumulating unbounded work on the host.
async fn relay(app: App) {
    let Some(base) = app.cfg.central_url.clone() else {
        return;
    };
    let url = format!("{}/harness/relay", base.trim_end_matches('/'))
        .replacen("https://", "wss://", 1)
        .replacen("http://", "ws://", 1);
    loop {
        // Tungstenite correctly enforces HTTP/1.1 for production WSS peers.
        // The reference contract hub is deliberately a stdlib HTTP/1.0
        // server, so use the protocol's tiny framed subset for plain WS.
        if url.starts_with("ws://") {
            if let Err(error) = relay_plain(&url, &app).await {
                warn!("plain relay connection failed: {error}");
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        }
        let request = match url.clone().into_client_request().map(|mut request| {
            request.headers_mut().insert(
                header::AUTHORIZATION,
                HeaderValue::from_str(&format!("Bearer {}", app.cfg.token))
                    .unwrap_or(HeaderValue::from_static("")),
            );
            request.headers_mut().insert(
                "X-Drover-Host-ID",
                HeaderValue::from_str(&app.cfg.host_id).unwrap_or(HeaderValue::from_static("")),
            );
            request
        }) {
            Ok(request) => request,
            Err(error) => {
                warn!("invalid relay URL: {error}");
                return;
            }
        };
        match connect_async(request).await {
            Ok((mut socket, _)) => {
                let hello = json!({"kind":"hello","host_id":app.cfg.host_id,"capabilities":["framed_responses_v1"]});
                if socket.send(Message::Text(hello.to_string())).await.is_err() {
                    continue;
                }
                while let Some(Ok(Message::Text(frame))) = socket.next().await {
                    let Ok(frame) = serde_json::from_str::<Value>(&frame) else {
                        continue;
                    };
                    match frame.get("kind").and_then(Value::as_str) {
                        Some("req") => {
                            let id = frame.get("id").cloned().unwrap_or(Value::Null);
                            let path = frame.get("path").and_then(Value::as_str).unwrap_or("");
                            let method =
                                frame.get("method").and_then(Value::as_str).unwrap_or("GET");
                            let (status, body) = if method == "GET" && path == "/sessions" {
                                (200, relay_inventory(&app).await)
                            } else {
                                (404, json!({"error":"not found"}))
                            };
                            let response = json!({"kind":"res","id":id,"status":status,"body":body.to_string()});
                            if socket
                                .send(Message::Text(response.to_string()))
                                .await
                                .is_err()
                            {
                                break;
                            }
                        }
                        Some("open") => {
                            let chan = frame.get("chan").cloned().unwrap_or(Value::Null);
                            let response = json!({"kind":"open_error","chan":chan,"error":"terminal unavailable"});
                            if socket
                                .send(Message::Text(response.to_string()))
                                .await
                                .is_err()
                            {
                                break;
                            }
                        }
                        _ => {}
                    }
                }
            }
            Err(error) => warn!("relay connection failed: {error}"),
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

async fn relay_plain(url: &str, app: &App) -> Result<()> {
    let parsed = url::Url::parse(url)?;
    let host = parsed.host_str().context("relay URL has no host")?;
    let port = parsed
        .port_or_known_default()
        .context("relay URL has no port")?;
    let mut socket = tokio::net::TcpStream::connect((host, port)).await?;
    let key = STANDARD.encode(Uuid::new_v4().as_bytes());
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {}\r\nAuthorization: Bearer {}\r\nX-Drover-Host-ID: {}\r\n\r\n",
        parsed.path(), host, port, key, app.cfg.token, app.cfg.host_id
    );
    socket.write_all(request.as_bytes()).await?;
    let mut header = Vec::new();
    while !header.ends_with(b"\r\n\r\n") {
        anyhow::ensure!(header.len() < 65_536, "relay handshake too large");
        let mut byte = [0_u8; 1];
        socket.read_exact(&mut byte).await?;
        header.push(byte[0]);
    }
    anyhow::ensure!(
        String::from_utf8_lossy(&header).contains(" 101 "),
        "relay handshake refused"
    );
    ws_send(
        &mut socket,
        &json!({"kind":"hello","host_id":app.cfg.host_id,"capabilities":["framed_responses_v1"]})
            .to_string(),
    )
    .await?;
    let (output_tx, mut output_rx) = mpsc::channel::<(String, Value)>(TERMINAL_CHANNEL_CAPACITY);
    let mut channels: HashMap<String, (String, tokio::task::JoinHandle<()>)> = HashMap::new();
    loop {
        let frame = tokio::select! {
            outbound = output_rx.recv() => {
                let Some((chan, message)) = outbound else { continue };
                ws_send(&mut socket, &json!({"kind":"data","chan":chan,"message":message}).to_string()).await?;
                continue;
            }
            inbound = ws_recv(&mut socket) => inbound?,
        };
        let Some(frame) = frame else { break };
        let Ok(frame) = serde_json::from_str::<Value>(&frame) else {
            continue;
        };
        match frame.get("kind").and_then(Value::as_str) {
            Some("req") => {
                let id = frame.get("id").cloned().unwrap_or(Value::Null);
                let path = frame.get("path").and_then(Value::as_str).unwrap_or("");
                let method = frame.get("method").and_then(Value::as_str).unwrap_or("GET");
                let (status, body) = if method == "GET" && path == "/sessions" {
                    (200, relay_inventory(app).await)
                } else {
                    (404, json!({"error":"not found"}))
                };
                ws_send(
                    &mut socket,
                    &json!({"kind":"res","id":id,"status":status,"body":body.to_string()})
                        .to_string(),
                )
                .await?;
            }
            Some("open") => {
                let chan = frame
                    .get("chan")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let path = frame.get("path").and_then(Value::as_str).unwrap_or("");
                let session_id = path
                    .strip_prefix("/sessions/")
                    .and_then(|v| v.strip_suffix("/terminal"));
                let terminals = app.terminals.read().await;
                let terminal = match session_id.and_then(|id| terminals.get(id).cloned()) {
                    Some(terminal) => terminal,
                    None => {
                        ws_send(&mut socket, &json!({"kind":"open_error","chan":chan,"error":"unknown terminal session"}).to_string()).await?;
                        continue;
                    }
                };
                drop(terminals);
                ws_send(
                    &mut socket,
                    &json!({"kind":"opened","chan":chan}).to_string(),
                )
                .await?;
                ws_send(&mut socket, &json!({"kind":"data","chan":chan,"message":{"type":"attached","session_id":session_id}}).to_string()).await?;
                if let Some(message) = terminal.replay() {
                    ws_send(
                        &mut socket,
                        &json!({"kind":"data","chan":chan,"message":message}).to_string(),
                    )
                    .await?;
                }
                let tx = output_tx.clone();
                let relay_chan = chan.clone();
                let mut subscription = terminal.subscribe();
                let task = tokio::spawn(async move {
                    loop {
                        match subscription.recv().await {
                            Ok(message) => {
                                if tx.try_send((relay_chan.clone(), message)).is_err() {
                                    break;
                                }
                            }
                            Err(broadcast::error::RecvError::Lagged(_)) => continue,
                            Err(_) => break,
                        }
                    }
                });
                channels.insert(chan, (session_id.unwrap_or_default().to_string(), task));
            }
            Some("data") => {
                let chan = frame.get("chan").and_then(Value::as_str).unwrap_or("");
                let Some((session_id, _)) = channels.get(chan) else {
                    continue;
                };
                let message = frame.get("message").unwrap_or(&Value::Null);
                let terminal = app.terminals.read().await.get(session_id).cloned();
                if let Some(terminal) = terminal {
                    match message.get("type").and_then(Value::as_str) {
                        Some("input") => {
                            let _ = terminal.write(
                                message
                                    .get("data")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .as_bytes(),
                            );
                        }
                        Some("interrupt") => {
                            let _ = terminal.write(b"\x03");
                        }
                        Some("ping") => {
                            ws_send(
                                &mut socket,
                                &json!({"kind":"data","chan":chan,"message":{"type":"pong"}})
                                    .to_string(),
                            )
                            .await?;
                        }
                        _ => {}
                    }
                }
            }
            Some("close") => {
                if let Some((_, task)) =
                    channels.remove(frame.get("chan").and_then(Value::as_str).unwrap_or(""))
                {
                    task.abort();
                }
            }
            _ => {}
        }
    }
    Ok(())
}

async fn ws_send(socket: &mut tokio::net::TcpStream, text: &str) -> Result<()> {
    let bytes = text.as_bytes();
    anyhow::ensure!(bytes.len() <= 8 << 20, "relay frame too large");
    let mut head = vec![0x81];
    if bytes.len() < 126 {
        head.push(0x80 | bytes.len() as u8);
    } else if bytes.len() <= u16::MAX as usize {
        head.push(0x80 | 126);
        head.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
    } else {
        head.push(0x80 | 127);
        head.extend_from_slice(&(bytes.len() as u64).to_be_bytes());
    }
    let mask = Uuid::new_v4();
    let mask = &mask.as_bytes()[..4];
    head.extend_from_slice(mask);
    socket.write_all(&head).await?;
    let masked: Vec<u8> = bytes
        .iter()
        .enumerate()
        .map(|(index, byte)| byte ^ mask[index % 4])
        .collect();
    socket.write_all(&masked).await?;
    Ok(())
}

async fn ws_recv(socket: &mut tokio::net::TcpStream) -> Result<Option<String>> {
    let mut first = [0_u8; 2];
    if socket.read_exact(&mut first).await.is_err() {
        return Ok(None);
    }
    let opcode = first[0] & 0x0f;
    let masked = first[1] & 0x80 != 0;
    let mut length = (first[1] & 0x7f) as u64;
    if length == 126 {
        let mut value = [0; 2];
        socket.read_exact(&mut value).await?;
        length = u16::from_be_bytes(value) as u64;
    } else if length == 127 {
        let mut value = [0; 8];
        socket.read_exact(&mut value).await?;
        length = u64::from_be_bytes(value);
    }
    anyhow::ensure!(length <= 8 << 20, "relay frame too large");
    let mut mask = [0; 4];
    if masked {
        socket.read_exact(&mut mask).await?;
    };
    let mut payload = vec![0; length as usize];
    socket.read_exact(&mut payload).await?;
    if masked {
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % 4];
        }
    }
    if opcode == 8 {
        return Ok(None);
    };
    if opcode != 1 {
        return Ok(Some(String::new()));
    };
    Ok(Some(String::from_utf8(payload)?))
}

async fn relay_inventory(app: &App) -> Value {
    let mut sessions: Vec<_> = app
        .sessions
        .read()
        .await
        .values()
        .filter(|session| session.status == "running")
        .cloned()
        .map(session_wire)
        .collect();
    sessions.sort_by(|a, b| b["created_at"].as_str().cmp(&a["created_at"].as_str()));
    json!({"host_id":app.cfg.host_id,"sessions":sessions,"archived_limit":20,"next_archived_cursor":null})
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    tracing_subscriber::fmt()
        .with_env_filter("warn")
        .with_writer(std::io::stderr)
        .init();
    let cfg = config(&args)?;
    let dbpath = cfg.home.join(".drover/harnessd/harnessd.sqlite");
    let db = init_db(&dbpath)?;
    let sessions = load_sessions(&db)?;
    let app = App {
        cfg: cfg.clone(),
        db: Arc::new(Mutex::new(db)),
        sessions: Arc::new(RwLock::new(sessions)),
        terminals: Arc::new(RwLock::new(HashMap::new())),
        client: Client::builder().timeout(Duration::from_secs(10)).build()?,
    };
    if let Some(path) = args.import_jsonl {
        let counts = import_jsonl(&app, &path, args.import_dry_run)?;
        println!(
            "IMPORT sessions={} events={} skipped={} dry_run={}",
            counts.0, counts.1, counts.2, args.import_dry_run
        );
        return Ok(());
    }
    let router = Router::new()
        .route("/healthz", get(health))
        .route("/capabilities", get(capabilities))
        .route(
            "/sessions",
            get(sessions_get).post(sessions_create).options(options),
        )
        .route("/sessions/:id", get(session_get).delete(terminate))
        .route("/sessions/:id/terminate", post(terminate))
        .route("/sessions/:id/turns", post(turn))
        .route("/fs/list", get(fs_list))
        .route("/fs/create", post(fs_create))
        .route("/fs/exists", post(fs_exists))
        .route("/model-catalog", get(catalog))
        .route("/native-sessions", get(native_sessions))
        .fallback(any(fallback))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind(&args.listen).await?;
    println!("LISTENING {}", listener.local_addr()?);
    std::io::stdout().flush()?;
    tokio::spawn(heartbeat(app.clone()));
    if cfg.relay {
        tokio::spawn(relay(app.clone()));
    }
    tokio::spawn(delivery::run(app));
    axum::serve(listener, router).await?;
    Ok(())
}
fn import_jsonl(app: &App, path: &FsPath, dry_run: bool) -> Result<(usize, usize, usize)> {
    let mut sessions = 0;
    let mut events = 0;
    let mut skipped = 0;
    for line in fs::read_to_string(path)?.lines() {
        let v: Value = serde_json::from_str(line)?;
        let kind = v
            .get("kind")
            .or_else(|| v.get("record_type"))
            .and_then(Value::as_str);
        if kind == Some("session") {
            let row = v.get("session").unwrap_or(&v);
            let session_id = row.get("session_id").and_then(Value::as_str).unwrap_or("");
            if session_id.is_empty() {
                skipped += 1;
                continue;
            }
            let session = Session {
                session_id: session_id.to_string(),
                harness: row
                    .get("harness")
                    .and_then(Value::as_str)
                    .unwrap_or("shell")
                    .to_string(),
                mode: row
                    .get("mode")
                    .and_then(Value::as_str)
                    .unwrap_or("pty")
                    .to_string(),
                cwd: row
                    .get("cwd")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                status: row
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("errored")
                    .to_string(),
                pid: None,
                model: row.get("model").and_then(Value::as_str).map(str::to_string),
                thinking_effort: row
                    .get("thinking_effort")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                client_session_id: row
                    .get("client_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                parent_session_id: row
                    .get("parent_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                native_session_id: row
                    .get("native_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                worktree: row
                    .get("worktree")
                    .cloned()
                    .and_then(|x| serde_json::from_value(x).ok()),
                awaiting: row
                    .get("awaiting")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                created_at: row
                    .get("created_at")
                    .or_else(|| row.get("started_at"))
                    .and_then(Value::as_str)
                    .unwrap_or("1970-01-01T00:00:00+00:00")
                    .to_string(),
            };
            sessions += 1;
            if !dry_run {
                app.save_session(&session)?;
            }
            continue;
        }
        if kind.is_some_and(|k| k != "event") {
            skipped += 1;
            continue;
        }
        if v.get("event_id").is_none() || v.get("dedup_key").is_none() {
            skipped += 1;
            continue;
        }
        if dry_run || app.import_event(&v)? {
            events += 1;
        }
    }
    Ok((sessions, events, skipped))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn event_hash_matches_python_vector() {
        let v = json!({"event_id":"new","aggregated_output":"drop","nested":{"event_id":"nested","ok":true}});
        assert_eq!(
            event_identity("s-1", 7, "tool_result", "2026-01-02T03:04:05+00:00", &v),
            "2b707256b02eac2ef61f23ffabb3c232899851bc38cd933ed163d79e4713cfad"
        );
    }
}
