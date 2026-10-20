//! Folder picker operations pin each directory component with O_NOFOLLOW.
use super::*;
use std::{
    ffi::{CStr, CString},
    os::fd::{AsRawFd, FromRawFd},
    os::unix::fs::MetadataExt,
};

fn roots(app: &App) -> Vec<PathBuf> {
    std::env::var_os("DROVER_FOLDER_ROOTS")
        .map(|v| std::env::split_paths(&v).collect())
        .unwrap_or_else(|| vec![app.cfg.home.clone()])
        .into_iter()
        .filter(|p| p.is_absolute() && directory(p).is_ok())
        .collect()
}
fn directory(path: &FsPath) -> std::result::Result<fs::File, &'static str> {
    let mut file = fs::File::open("/").map_err(|_| "not_found")?;
    for part in path.components() {
        match part {
            std::path::Component::RootDir => continue,
            std::path::Component::Normal(name) => {
                let name = CString::new(name.as_encoded_bytes()).map_err(|_| "invalid_path")?;
                // SAFETY: the parent descriptor and NUL-terminated name live
                // through openat; ownership of a successful fd transfers once.
                let fd = unsafe {
                    libc::openat(
                        file.as_raw_fd(),
                        name.as_ptr(),
                        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    )
                };
                if fd < 0 {
                    return Err("symlink_not_allowed");
                }
                file = unsafe { fs::File::from_raw_fd(fd) };
            }
            _ => return Err("outside_allowed_folders"),
        }
    }
    Ok(file)
}
fn authorize(app: &App, typed: &str) -> std::result::Result<(PathBuf, fs::File), &'static str> {
    if typed.len() > 4096 || typed.contains('\0') {
        return Err("invalid_path");
    }
    let expanded = if typed == "~" {
        app.cfg.home.clone()
    } else if let Some(rest) = typed.strip_prefix("~/") {
        app.cfg.home.join(rest)
    } else {
        PathBuf::from(typed)
    };
    if !expanded.is_absolute() {
        return Err("invalid_path");
    }
    if typed.split('/').any(|p| p == "..") {
        return Err("outside_allowed_folders");
    }
    let roots = roots(app);
    let root = roots
        .iter()
        .filter(|r| expanded.starts_with(r))
        .max_by_key(|r| r.components().count())
        .ok_or("outside_allowed_folders")?;
    if expanded
        .strip_prefix(root)
        .unwrap()
        .components()
        .any(|p| p.as_os_str().as_encoded_bytes().starts_with(b"."))
    {
        return Err("hidden_folder_not_allowed");
    }
    let file = directory(&expanded)?;
    Ok((expanded, file))
}
fn failure(code: &str) -> Response {
    err(
        if matches!(
            code,
            "outside_allowed_folders" | "hidden_folder_not_allowed" | "symlink_not_allowed"
        ) {
            StatusCode::FORBIDDEN
        } else {
            StatusCode::BAD_REQUEST
        },
        code,
    )
}
fn names(
    file: &fs::File,
    offset: usize,
    filter: &str,
    limit: usize,
) -> Result<(Vec<String>, Option<usize>)> {
    // fdopendir takes ownership of its fd, so duplicate the caller's pinned fd.
    let fd = unsafe { libc::dup(file.as_raw_fd()) };
    anyhow::ensure!(fd >= 0, "duplicate directory fd");
    let dir = unsafe { libc::fdopendir(fd) };
    if dir.is_null() {
        unsafe { libc::close(fd) };
        anyhow::bail!("open directory stream");
    }
    let mut names = Vec::new();
    let mut scanned = 0;
    let mut next = None;
    loop {
        let entry = unsafe { libc::readdir(dir) };
        if entry.is_null() {
            break;
        }
        let position = scanned;
        scanned += 1;
        if position < offset {
            continue;
        }
        if names.len() >= limit || position - offset >= 5000 {
            next = Some(position);
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }
            .to_string_lossy()
            .into_owned();
        if name.starts_with('.') || !name.to_lowercase().contains(&filter.to_lowercase()) {
            continue;
        }
        let c = CString::new(name.as_bytes())?;
        let child = unsafe {
            libc::openat(
                file.as_raw_fd(),
                c.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if child >= 0 {
            unsafe { libc::close(child) };
            names.push(name);
        }
    }
    unsafe { libc::closedir(dir) };
    Ok((names, next))
}
pub(super) async fn list(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    if q.get("host_id") != Some(&app.cfg.host_id) {
        return err(StatusCode::FORBIDDEN, "host mismatch");
    }
    let limit = match q.get("limit").map(|v| v.parse::<usize>()) {
        Some(Ok(n)) if (1..=100).contains(&n) => n,
        None => 100,
        _ => return failure("invalid_limit"),
    };
    let root_rows=roots(&app).into_iter().map(|p|json!({"name":if p==app.cfg.home {"Home".to_owned()} else {p.file_name().unwrap_or_default().to_string_lossy().into_owned()},"path":p})).collect::<Vec<_>>();
    let path = q.get("path").map(String::as_str).unwrap_or("");
    if path.is_empty() {
        return ok(
            json!({"roots":root_rows,"path":null,"parent":null,"entries":[],"truncated":false,"next_cursor":null}),
        );
    }
    let (path, file) = match authorize(&app, path) {
        Ok(v) => v,
        Err(e) => return failure(e),
    };
    let filter = q.get("filter").cloned().unwrap_or_default();
    if filter.len() > 256 {
        return failure("invalid_path");
    }
    let metadata = match file.metadata() {
        Ok(v) => v,
        Err(_) => return failure("not_found"),
    };
    let identity = json!([
        metadata.dev(),
        metadata.ino(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec()
    ]);
    let mut offset = 0;
    if let Some(cursor) = q.get("cursor").filter(|v| !v.is_empty()) {
        let value = URL_SAFE
            .decode(cursor)
            .ok()
            .and_then(|v| serde_json::from_slice::<Value>(&v).ok());
        let Some(value) = value else {
            return failure("invalid_cursor");
        };
        if cursor.len() > 2048
            || value["path"] != json!(path)
            || value["filter"] != filter
            || value["identity"] != identity
        {
            return failure("invalid_cursor");
        }
        let Some(n) = value["offset"].as_u64() else {
            return failure("invalid_cursor");
        };
        offset = n as usize;
    }
    let (names, next) = match names(&file, offset, &filter, limit) {
        Ok(v) => v,
        Err(_) => return failure("not_found"),
    };
    let truncated = next.is_some();
    let rows=names.into_iter().map(|name|{let full=path.join(&name);json!({"name":name,"path":full,"is_dir":true,"is_git_repo":full.join(".git").exists(),"hidden":false})}).collect::<Vec<_>>();
    let cursor = next.map(|offset| {
        URL_SAFE.encode(
            json!({"path":path,"filter":filter,"identity":identity,"offset":offset}).to_string(),
        )
    });
    let parent = path
        .parent()
        .filter(|p| roots(&app).iter().any(|r| p.starts_with(r)));
    ok(
        json!({"roots":root_rows,"path":path,"parent":parent,"entries":rows,"truncated":truncated,"next_cursor":cursor}),
    )
}
pub(super) async fn create(
    State(app): State<App>,
    headers: HeaderMap,
    Json(v): Json<Value>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    if v["host_id"].as_str() != Some(&app.cfg.host_id) {
        return err(StatusCode::FORBIDDEN, "host mismatch");
    }
    let name = v["name"].as_str().unwrap_or("");
    if name.is_empty() || name.contains('/') || name.contains('\0') || name.starts_with('.') {
        return failure("invalid_name");
    }
    let (path, file) = match authorize(&app, v["path"].as_str().unwrap_or("")) {
        Ok(v) => v,
        Err(e) => return failure(e),
    };
    let c = CString::new(name).unwrap();
    if unsafe { libc::mkdirat(file.as_raw_fd(), c.as_ptr(), 0o755) } == 0 {
        return response(
            StatusCode::CREATED,
            json!({"name":name,"path":path.join(name)}),
        );
    }
    if std::io::Error::last_os_error().kind() == std::io::ErrorKind::AlreadyExists {
        err(StatusCode::CONFLICT, "folder_exists")
    } else {
        failure("unavailable")
    }
}
pub(super) async fn exists(
    State(app): State<App>,
    headers: HeaderMap,
    Json(v): Json<Value>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let mut result = serde_json::Map::new();
    if let Some(paths) = v["paths"].as_array() {
        if paths.len() > 100 {
            return failure("invalid_paths");
        }
        for path in paths.iter().filter_map(Value::as_str) {
            result.insert(path.into(), json!(authorize(&app, path).is_ok()));
        }
    }
    ok(json!({"exists":result}))
}
pub(super) async fn complete(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let typed = q.get("path").map(String::as_str).unwrap_or("");
    let expanded = if let Some(rest) = typed.strip_prefix("~/") {
        app.cfg.home.join(rest)
    } else {
        PathBuf::from(typed)
    };
    let (parent, partial) = if typed.ends_with('/') {
        (expanded.clone(), String::new())
    } else {
        (
            expanded.parent().unwrap_or(&app.cfg.home).to_path_buf(),
            expanded
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned(),
        )
    };
    let (path, file) = match authorize(&app, &parent.to_string_lossy()) {
        Ok(v) => v,
        Err(e) => return ok(json!({"parent":parent,"entries":[],"truncated":false,"error":e})),
    };
    let (names, next) = names(&file, 0, &partial, 100).unwrap_or_default();
    let entries = names
        .into_iter()
        .filter(|n| n.to_lowercase().starts_with(&partial.to_lowercase()))
        .map(|name| json!({"path":path.join(&name),"name":name}))
        .collect::<Vec<_>>();
    ok(json!({"parent":path,"entries":entries,"truncated":next.is_some()}))
}
