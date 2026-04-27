// Tauri shell entry. Spawns the window and exposes a single command for the
// webview to introspect what version of the shell it's running against.
//
// Sidecar wiring (spawn ingester + agent-service binaries) lands in P5/P6
// once PyInstaller / pkg bundling is in place. P0 expects the user to run
// the sidecars manually.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[tauri::command]
fn shell_info() -> serde_json::Value {
    serde_json::json!({
        "shell": "mailmind",
        "version": env!("CARGO_PKG_VERSION"),
        "tauri_version": tauri::VERSION,
    })
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![shell_info])
        .run(tauri::generate_context!())
        .expect("error while running mailmind shell");
}
