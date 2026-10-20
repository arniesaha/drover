//! HTTP and both relay transports dispatch through the same router and PTY.
use super::*;
use axum::body::{to_bytes, Body};
use tower::ServiceExt;

fn terminal_input(terminal: &Terminal, message: &Value) -> Result<Option<Value>> {
    match message["type"].as_str() {
        Some("input") => terminal.write(message["data"].as_str().unwrap_or("").as_bytes())?,
        Some("interrupt") => terminal.write(b"\x03")?,
        Some("resize") => terminal
            ._master
            .lock()
            .unwrap()
            .resize(portable_pty::PtySize {
                rows: message["rows"].as_u64().unwrap_or(24).clamp(1, 1000) as u16,
                cols: message["cols"].as_u64().unwrap_or(80).clamp(1, 1000) as u16,
                pixel_width: 0,
                pixel_height: 0,
            })?,
        Some("ping") => return Ok(Some(json!({"type":"pong"}))),
        _ => {}
    }
    Ok(None)
}

pub(super) async fn terminal_upgrade(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
    upgrade: Option<WebSocketUpgrade>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let Some(terminal) = app.terminals.read().await.get(&id).cloned() else {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown terminal session: {id}"),
        );
    };
    let Some(upgrade) = upgrade else {
        return err(StatusCode::BAD_REQUEST, "websocket upgrade required");
    };
    upgrade
        .max_message_size(1024 * 1024)
        .on_upgrade(move |socket| terminal_socket(socket, terminal, id, app))
        .into_response()
}
async fn terminal_socket(mut socket: WebSocket, terminal: Arc<Terminal>, id: String, app: App) {
    use axum::extract::ws::Message as WsMessage;
    let mut output = terminal.subscribe();
    let mut events = app.events.subscribe();
    if socket
        .send(WsMessage::Text(
            json!({"type":"attached","session_id":id}).to_string(),
        ))
        .await
        .is_err()
    {
        return;
    }
    if let Some(replay) = terminal.replay() {
        let _ = socket.send(WsMessage::Text(replay.to_string())).await;
    }
    loop {
        let value = tokio::select! {
            frame = socket.recv() => match frame {
                Some(Ok(WsMessage::Text(text))) => {
                    let Ok(message) = serde_json::from_str::<Value>(&text) else { continue; };
                    if matches!(message["type"].as_str(),Some("close"|"detach")) { break; }
                    match terminal_input(&terminal,&message) { Ok(Some(v))=>v, Ok(None)=>continue,Err(_)=>break }
                }
                Some(Ok(WsMessage::Ping(bytes))) => { let _ = socket.send(WsMessage::Pong(bytes)).await; continue; }
                Some(Ok(WsMessage::Pong(_))) => continue,
                _ => break,
            },
            message = output.recv() => match message { Ok(v)=>v,Err(broadcast::error::RecvError::Lagged(_))=>continue,Err(_)=>break },
            message = events.recv() => match message { Ok(v) if v["event"]["session_id"]==id=>v,_=>continue },
        };
        if socket
            .send(WsMessage::Text(value.to_string()))
            .await
            .is_err()
        {
            break;
        }
    }
}

async fn dispatch(app: App, frame: Value) -> Value {
    let id = frame["id"].clone();
    let path = frame["path"].as_str().unwrap_or("/");
    let method = frame["method"].as_str().unwrap_or("GET");
    let body = frame["body"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| {
            if frame["body"].is_null() {
                String::new()
            } else {
                frame["body"].to_string()
            }
        });
    let request = axum::http::Request::builder()
        .method(method)
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {}", app.cfg.token))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body));
    let max_bytes = frame["max_response_bytes"]
        .as_u64()
        .unwrap_or(16 * 1024 * 1024)
        .min(16 * 1024 * 1024) as usize;
    let (status, body) = match request {
        Ok(request) => {
            match tokio::time::timeout(Duration::from_secs(15), router(app).oneshot(request)).await
            {
                Ok(Ok(reply)) => {
                    let status = reply.status().as_u16();
                    match to_bytes(reply.into_body(), max_bytes).await {
                        Ok(bytes) => (status, String::from_utf8_lossy(&bytes).into_owned()),
                        Err(_) => (502, "{\"error\":\"relay response too large\"}".into()),
                    }
                }
                _ => (504, "{\"error\":\"relay request timed out\"}".into()),
            }
        }
        Err(_) => (400, "{\"error\":\"invalid relay request\"}".into()),
    };
    json!({"kind":"res","id":id,"status":status,"body":body,"framed":frame["response_framing"]=="framed_responses_v1"})
}

pub(super) async fn relay(app: App) {
    let Some(base) = &app.cfg.central_url else {
        return;
    };
    let url = format!("{base}/harness/relay")
        .replacen("https://", "wss://", 1)
        .replacen("http://", "ws://", 1);
    let mut delay = 2;
    loop {
        if let Err(error) = connection(&app, &url).await {
            warn!("relay disconnected: {error}");
        }
        tokio::time::sleep(Duration::from_secs(delay)).await;
        delay = (delay * 2).min(300);
    }
}
async fn connection(app: &App, url: &str) -> Result<()> {
    let mut request = url.into_client_request()?;
    request.headers_mut().insert(
        header::AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {}", app.cfg.token))?,
    );
    request
        .headers_mut()
        .insert("X-Drover-Host-ID", HeaderValue::from_str(&app.cfg.host_id)?);
    let (mut socket, _) = connect_async(request).await?;
    socket.send(Message::Text(json!({"kind":"hello","host_id":app.cfg.host_id,"capabilities":["framed_responses_v1"]}).to_string())).await?;
    let (tx, mut rx) = mpsc::channel::<Value>(256);
    let permits = Arc::new(tokio::sync::Semaphore::new(16));
    let mut channels: HashMap<String, (Arc<Terminal>, tokio::task::JoinHandle<()>)> =
        HashMap::new();
    let mut requests = tokio::task::JoinSet::new();
    let result: Result<()> = async {
        loop {
            let frame=tokio::select! {
                output=rx.recv()=>{ if let Some(value)=output { if value["framed"]==true {
                            let body=value["body"].as_str().unwrap_or("");
                            socket.send(Message::Text(json!({"kind":"res_start","id":value["id"],"status":value["status"],"body_bytes":body.len()}).to_string())).await?;
                            socket.send(Message::Text(body.into())).await?;
                        } else {
                            let mut value=value;
                            if let Some(object)=value.as_object_mut() {object.remove("framed");}
                            socket.send(Message::Text(value.to_string())).await?;
                        } } continue; },
                _=requests.join_next(), if !requests.is_empty()=>{continue;},
                frame=tokio::time::timeout(Duration::from_secs(90),socket.next())=>match frame? {
                    Some(Ok(Message::Text(text)))=>serde_json::from_str::<Value>(&text)?,
                    Some(Ok(Message::Ping(bytes)))=>{socket.send(Message::Pong(bytes)).await?;continue;},
                    Some(Ok(Message::Pong(_)))=>continue,
                    _=>break,
                }
            };
            match frame["kind"].as_str() {
                Some("req")=>{
                    match permits.clone().try_acquire_owned() {
                        Ok(permit)=>{
                            let app=app.clone();let tx=tx.clone();
                            requests.spawn(async move { let _permit=permit; let reply=dispatch(app,frame).await; let _=tx.send(reply).await; });
                        }
                        Err(_)=>socket.send(Message::Text(json!({"kind":"res","id":frame["id"],"status":503,"body":"{\"error\":\"relay saturated\"}"}).to_string())).await?,
                    }
                }
                Some("open")=>{
                    let chan=frame["chan"].as_str().unwrap_or("").to_string();
                    let sid=frame["path"].as_str().and_then(|v|v.strip_prefix("/sessions/")).and_then(|v|v.strip_suffix("/terminal")).unwrap_or("");
                    let terminal=app.terminals.read().await.get(sid).cloned();
                    if channels.len()>=64 || terminal.is_none() {
                        socket.send(Message::Text(json!({"kind":"open_error","chan":chan,"error":"unknown terminal session or channel limit"}).to_string())).await?;continue;
                    }
                    let terminal=terminal.unwrap();
                    let mut subscription=terminal.subscribe();
                    socket.send(Message::Text(json!({"kind":"opened","chan":chan}).to_string())).await?;
                    socket.send(Message::Text(json!({"kind":"data","chan":chan,"message":{"type":"attached","session_id":sid}}).to_string())).await?;
                    if let Some(replay)=terminal.replay() { socket.send(Message::Text(json!({"kind":"data","chan":chan,"message":replay}).to_string())).await?; }
                    let tx=tx.clone();let channel=chan.clone();
                    let task=tokio::spawn(async move {
                        loop {
                            match subscription.recv().await {
                                Ok(message)=>{if tx.try_send(json!({"kind":"data","chan":channel,"message":message})).is_err() {break;}},
                                Err(broadcast::error::RecvError::Lagged(_))=>continue,
                                Err(_)=>break,
                            }
                        }
                    });
                    if let Some((_,old))=channels.insert(chan,(terminal,task)) {old.abort();}
                }
                Some("data")=>{
                    let chan=frame["chan"].as_str().unwrap_or("");
                    if let Some((terminal,_))=channels.get(chan) {
                        if let Some(message)=terminal_input(terminal,&frame["message"])? {
                            socket.send(Message::Text(json!({"kind":"data","chan":chan,"message":message}).to_string())).await?;
                        }
                    }
                }
                Some("close")=>{if let Some((_,task))=channels.remove(frame["chan"].as_str().unwrap_or("")) {task.abort();}},
                _=>{}
            }
        }
        Ok(())
    }.await;
    for (_, (_, task)) in channels {
        task.abort();
    }
    requests.abort_all();
    result
}
