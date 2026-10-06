// ProdAI Atlas 데스크톱 (PRD §11.17): atlas-manager 를 사이드카로 띄우고(127.0.0.1 임의 포트 · SQLite 앱 데이터),
// /health 가 뜨면 창을 Manager 가 내장 서빙하는 콘솔로 옮긴다. 종료 시 사이드카도 정리.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(18080)
}

// 외부 크레이트 없이 GET /health → 200 여부
fn healthy(port: u16) -> bool {
    let Ok(mut s) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(300)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(1)));
    if s.write_all(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_err() {
        return false;
    }
    let mut buf = [0u8; 16];
    matches!(s.read(&mut buf), Ok(n) if n >= 12 && &buf[9..12] == b"200")
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let port = free_port();
            let data = app.path().app_data_dir()?;
            std::fs::create_dir_all(&data)?;
            let db = data.join("atlas.db");
            let (mut rx, child) = app
                .shell()
                .sidecar("atlas-manager")?
                .env("ATLAS_LISTEN", format!("127.0.0.1:{port}"))
                .env("APP_ENV", "desktop")
                .env("GIN_MODE", "release")
                .env("DB_ENGINE", "sqlite")
                .env("DATABASE_URL", format!("file:{}", db.display()))
                .spawn()?;
            app.manage(Sidecar(Mutex::new(Some(child))));
            // 사이드카 로그를 표준출력으로 (파이프가 차서 멈추지 않도록 계속 비운다)
            tauri::async_runtime::spawn(async move {
                while let Some(ev) = rx.recv().await {
                    if let CommandEvent::Stdout(l) | CommandEvent::Stderr(l) = ev {
                        print!("[atlas-manager] {}", String::from_utf8_lossy(&l));
                    }
                }
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for _ in 0..150 {
                    if healthy(port) {
                        println!("atlas-desktop: manager ready on 127.0.0.1:{port}");
                        if let (Some(w), Ok(url)) = (handle.get_webview_window("main"), format!("http://127.0.0.1:{port}/").parse()) {
                            let _ = w.navigate(url);
                        }
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                eprintln!("atlas-desktop: manager did not become healthy");
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("tauri app")
        .run(|app, ev| {
            if let RunEvent::Exit = ev {
                if let Some(child) = app.state::<Sidecar>().0.lock().ok().and_then(|mut c| c.take()) {
                    let _ = child.kill();
                }
            }
        });
}
