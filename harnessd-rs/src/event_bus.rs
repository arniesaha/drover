//! Bounded event ingress, one durable writer, then bounded subscriber fan-out.
use super::*;

struct Record {
    session_id: String,
    kind: String,
    text: Option<String>,
    payload: Value,
    turn_id: Option<String>,
    reply: std::sync::mpsc::SyncSender<std::result::Result<(), String>>,
}
#[derive(Clone)]
pub(super) struct StoreWriter(std::sync::mpsc::SyncSender<Record>);
impl StoreWriter {
    pub fn start(db: Arc<Mutex<Connection>>, bus: broadcast::Sender<Value>) -> Result<Self> {
        let (tx, rx) = std::sync::mpsc::sync_channel::<Record>(256);
        std::thread::Builder::new()
            .name("event-store-writer".into())
            .spawn(move || {
                for record in rx {
                    let result = persist(&mut db.lock().unwrap(), &record);
                    match result {
                        Ok(event) => {
                            let _ = bus.send(json!({"type":"event","event":event}));
                            let _ = record.reply.send(Ok(()));
                        }
                        Err(error) => {
                            error!("event store unavailable: {error}");
                            let _ = record.reply.send(Err(error.to_string()));
                        }
                    }
                }
            })?;
        Ok(Self(tx))
    }
    pub fn append(
        &self,
        session_id: &str,
        kind: &str,
        text: Option<&str>,
        payload: Value,
        turn_id: Option<&str>,
    ) -> Result<()> {
        let (reply, result) = std::sync::mpsc::sync_channel(1);
        self.0.send(Record {
            session_id: session_id.into(),
            kind: kind.into(),
            text: text.map(str::to_owned),
            payload,
            turn_id: turn_id.map(str::to_owned),
            reply,
        })?;
        result.recv()?.map_err(anyhow::Error::msg)
    }
}
fn persist(db: &mut Connection, record: &Record) -> Result<Value> {
    let tx = db.transaction()?;
    tx.execute(
        "INSERT OR IGNORE INTO session_sequences(session_id,next_seq) VALUES(?1,0)",
        [&record.session_id],
    )?;
    let seq: i64 = tx.query_row(
        "UPDATE session_sequences SET next_seq=next_seq+1 WHERE session_id=?1 RETURNING next_seq-1",
        [&record.session_id],
        |r| r.get(0),
    )?;
    let ts = now();
    let event_id = format!("harness-event-{}", Uuid::new_v4());
    let dedup = event_identity(&record.session_id, seq, &record.kind, &ts, &record.payload);
    tx.execute("INSERT INTO events(event_id,session_id,seq,type,text,payload,turn_id,ts,dedup_key) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![event_id,record.session_id,seq,record.kind,record.text,record.payload.to_string(),record.turn_id,ts,dedup])?;
    prune_events(&tx)?;
    tx.commit()?;
    Ok(
        json!({"event_id":event_id,"session_id":record.session_id,"type":record.kind,"text":record.text,"payload":record.payload,"turn_id":record.turn_id,"seq":seq,"ts":ts}),
    )
}
