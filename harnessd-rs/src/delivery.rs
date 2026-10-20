//! One durable delivery path for live events and restart reconciliation.
use super::*;
use std::collections::HashSet;

const MAX_BYTES: usize = 15 * 1024 * 1024;
const REASONS: [&str; 2] = [
    "session_missing_after_registration_window",
    "session_owned_by_another_host",
];

fn tombstone(event: &Value) -> Value {
    let bytes = json!({"events":[event]}).to_string().len();
    let mut capped = json!({
        "event_id":event["event_id"], "session_id":event["session_id"],
        "type":event["type"], "seq":event["seq"],
        "payload":{"truncated":true,"reason":"event_size_cap","original_bytes":bytes}
    });
    if event["ts"].is_string() {
        capped["ts"] = event["ts"].clone();
    }
    capped
}

#[derive(Default, Debug)]
struct Ack {
    pending: HashSet<String>,
    rejected: HashMap<String, String>,
}
impl Ack {
    fn parse(body: &Value, offered: &HashSet<String>, status: u16) -> Result<Self> {
        anyhow::ensure!(body.is_object(), "invalid event acknowledgement");
        let strings = |key| -> Result<HashSet<String>> {
            match body.get(key) {
                None => Ok(HashSet::new()),
                Some(Value::Array(values)) => values
                    .iter()
                    .map(|v| {
                        v.as_str()
                            .map(str::to_owned)
                            .context("invalid acknowledgement session")
                    })
                    .collect(),
                _ => anyhow::bail!("invalid acknowledgement list"),
            }
        };
        let pending = strings("pending")?;
        let orphaned = strings("orphaned")?;
        let mut rejected = HashMap::new();
        if let Some(value) = body.get("rejected") {
            for (sid, reason) in value
                .as_object()
                .context("invalid rejected acknowledgement")?
            {
                let reason = reason.as_str().context("invalid rejection reason")?;
                anyhow::ensure!(REASONS.contains(&reason), "unknown rejection reason");
                rejected.insert(sid.clone(), reason.to_owned());
            }
        }
        for sid in orphaned {
            rejected.entry(sid).or_insert_with(|| REASONS[0].into());
        }
        anyhow::ensure!(pending.is_subset(offered), "unoffered pending session");
        anyhow::ensure!(
            rejected
                .keys()
                .all(|s| offered.contains(s) && !pending.contains(s)),
            "unoffered or contradictory rejection"
        );
        if status == 403 {
            anyhow::ensure!(
                (pending == *offered && rejected.is_empty())
                    || (pending.is_empty()
                        && rejected.keys().cloned().collect::<HashSet<_>>() == *offered),
                "authorization denied"
            );
        }
        Ok(Self { pending, rejected })
    }
}

/// Receipts and acknowledgements commit together. A failed receipt leaves the
/// entire page eligible for retry, and quarantine never counts as delivery.
fn commit_ack(db: &mut Connection, page: &[(i64, Value)], ack: &Ack) -> Result<()> {
    let tx = db.transaction()?;
    for (id, event) in page {
        let sid = event["session_id"]
            .as_str()
            .context("event session missing")?;
        if let Some(reason) = ack.rejected.get(sid) {
            tx.execute("INSERT OR IGNORE INTO event_quarantine(event_id,session_id,reason,quarantined_at) VALUES(?1,?2,?3,?4)", params![event["event_id"].as_str(),sid,reason,now()])?;
        } else if !ack.pending.contains(sid) {
            tx.execute(
                "UPDATE events SET pushed_at=?1 WHERE id=?2",
                params![now(), id],
            )?;
        }
    }
    tx.commit()?;
    for (sid, reason) in &ack.rejected {
        warn!("ownership quarantine: session_id={sid} reason={reason}");
    }
    Ok(())
}

fn outbox(
    db: &Connection,
    held: &HashMap<String, (tokio::time::Instant, u64)>,
) -> Result<Vec<(i64, Value)>> {
    let mut statement = db.prepare("SELECT id,event_id,session_id,type,text,payload,turn_id,seq,ts FROM events e WHERE pushed_at IS NULL AND NOT EXISTS(SELECT 1 FROM event_quarantine q WHERE q.event_id=e.event_id) ORDER BY id")?;
    let rows = statement.query_map([], |row| {
        Ok((row.get(0)?, json!({
            "event_id": row.get::<_, String>(1)?, "session_id":row.get::<_, String>(2)?,
            "type":row.get::<_, String>(3)?,"role":"assistant","text":row.get::<_, Option<String>>(4)?,
            "payload":serde_json::from_str::<Value>(&row.get::<_, String>(5)?).unwrap_or(Value::Null),
            "turn_id":row.get::<_, Option<String>>(6)?,"seq":row.get::<_, i64>(7)?,"ts":row.get::<_, String>(8)?
        })))
    })?;
    let mut page = Vec::new();
    let mut bytes = 16;
    for row in rows {
        let (id, mut event) = row?;
        if held
            .get(event["session_id"].as_str().unwrap_or(""))
            .is_some_and(|(until, _)| *until > tokio::time::Instant::now())
        {
            continue;
        }
        if json!({"events":[&event]}).to_string().len() > MAX_BYTES {
            event = tombstone(&event);
        }
        let size = event.to_string().len() + 1;
        if !page.is_empty() && (bytes + size > MAX_BYTES || page.len() == 100) {
            break;
        }
        bytes += size;
        page.push((id, event));
    }
    Ok(page)
}

pub(super) async fn run(app: App) {
    let Some(base) = &app.cfg.central_url else {
        return;
    };
    let mut held: HashMap<String, (tokio::time::Instant, u64)> = HashMap::new();
    let mut auth_delay = 30;
    loop {
        tokio::time::sleep(Duration::from_millis(250)).await;
        let page = match outbox(&app.db.lock().unwrap(), &held) {
            Ok(page) => page,
            Err(error) => {
                warn!("local reconciliation unavailable: {error}");
                continue;
            }
        };
        let mut pages = VecDeque::from([page]);
        while let Some(page) = pages.pop_front() {
            if page.is_empty() {
                continue;
            }
            let result = app
                .client
                .post(format!("{base}/harness/events"))
                .bearer_auth(&app.cfg.token)
                .header("X-Drover-Event-Delivery", "quarantine-v1")
                .json(&json!({"events":page.iter().map(|(_,e)|e).collect::<Vec<_>>()}))
                .send()
                .await;
            let response = match result {
                Ok(r) => r,
                Err(error) => {
                    warn!("event delivery unavailable: {error}");
                    tokio::time::sleep(Duration::from_secs(5)).await;
                    break;
                }
            };
            let status = response.status().as_u16();
            if status == 413 {
                if page.len() > 1 {
                    let mid = page.len() / 2;
                    pages.push_front(page[mid..].to_vec());
                    pages.push_front(page[..mid].to_vec());
                } else if page[0].1.pointer("/payload/reason").and_then(Value::as_str)
                    != Some("event_size_cap")
                {
                    pages.push_front(vec![(page[0].0, tombstone(&page[0].1))]);
                } else {
                    // Retain originals durably even when the hub cannot accept
                    // the minimum envelope; do not block unrelated sessions.
                    let ack = Ack {
                        rejected: HashMap::from([(
                            page[0].1["session_id"].as_str().unwrap().into(),
                            "event_size_cap_rejected".into(),
                        )]),
                        ..Ack::default()
                    };
                    if let Err(error) = commit_ack(&mut app.db.lock().unwrap(), &page, &ack) {
                        warn!("local quarantine unavailable: {error}");
                    }
                }
                continue;
            }
            let offered = page
                .iter()
                .filter_map(|(_, e)| e["session_id"].as_str().map(str::to_owned))
                .collect::<HashSet<_>>();
            let ack = if (200..300).contains(&status) || status == 403 {
                match response.bytes().await {
                    Ok(bytes) => serde_json::from_slice::<Value>(if bytes.is_empty() {
                        b"{}"
                    } else {
                        &bytes
                    })
                    .map_err(anyhow::Error::from)
                    .and_then(|body| Ack::parse(&body, &offered, status)),
                    Err(error) => Err(error.into()),
                }
            } else {
                Err(anyhow::anyhow!("event delivery HTTP {status}"))
            };
            match ack {
                Ok(ack) => {
                    let committed = commit_ack(&mut app.db.lock().unwrap(), &page, &ack);
                    if let Err(error) = committed {
                        warn!("local quarantine/acknowledgement unavailable: {error}");
                        break;
                    }
                    auth_delay = 30;
                    for sid in offered {
                        if ack.pending.contains(&sid) {
                            let delay = held.get(&sid).map(|(_, d)| *d).unwrap_or(30);
                            held.insert(
                                sid,
                                (
                                    tokio::time::Instant::now() + Duration::from_secs(delay),
                                    (delay * 2).min(300),
                                ),
                            );
                        } else {
                            held.remove(&sid);
                        }
                    }
                }
                Err(error) => {
                    if status == 401 || status == 403 {
                        warn!("event authorization backoff: {error}; retry_seconds={auth_delay}");
                        tokio::time::sleep(Duration::from_secs(auth_delay)).await;
                        auth_delay = (auth_delay * 2).min(300);
                    } else {
                        warn!("event reconciliation deferred: {error}");
                        tokio::time::sleep(Duration::from_secs(5)).await;
                    }
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_ownership_acknowledgements() {
        let offered = HashSet::from(["s".to_string()]);
        assert!(Ack::parse(&json!({"pending":["s"]}), &offered, 403).is_ok());
        assert!(Ack::parse(&json!({}), &offered, 403).is_err());
        assert!(Ack::parse(&json!({"pending":["other"]}), &offered, 200).is_err());
        assert!(Ack::parse(
            &json!({"rejected":{"s":"session_owned_by_another_host"}}),
            &offered,
            403
        )
        .is_ok());
        assert!(Ack::parse(
            &json!({"pending":["s"],"rejected":{"s":REASONS[0]}}),
            &offered,
            200
        )
        .is_err());
    }
    #[test]
    fn quarantine_survives_reopen_and_retention_and_receipt_failure_is_atomic() {
        let path =
            std::env::temp_dir().join(format!("harnessd-delivery-{}.sqlite", Uuid::new_v4()));
        let mut db = init_db(&path).unwrap();
        db.execute("INSERT INTO events(event_id,session_id,seq,type,payload,ts,dedup_key) VALUES('e','s',0,'status','{}','2000-01-01','d')",[]).unwrap();
        let page = vec![(1, json!({"event_id":"e","session_id":"s"}))];
        let ack = Ack {
            rejected: HashMap::from([("s".into(), REASONS[0].into())]),
            ..Ack::default()
        };
        db.execute_batch("CREATE TRIGGER fail_receipt BEFORE INSERT ON event_quarantine BEGIN SELECT RAISE(ABORT,'disk failure'); END;").unwrap();
        assert!(commit_ack(&mut db, &page, &ack).is_err());
        assert_eq!(outbox(&db, &HashMap::new()).unwrap().len(), 1);
        db.execute_batch("DROP TRIGGER fail_receipt").unwrap();
        commit_ack(&mut db, &page, &ack).unwrap();
        drop(db);
        let db = init_db(&path).unwrap();
        prune_events(&db).unwrap();
        assert!(outbox(&db, &HashMap::new()).unwrap().is_empty());
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM events WHERE pushed_at IS NULL",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
        drop(db);
        let _ = fs::remove_file(path);
    }
}
