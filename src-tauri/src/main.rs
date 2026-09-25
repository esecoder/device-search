// main.rs — the desktop shell: tray icon, global hotkey, floating search window.
//
// ⚠️ THIS FILE IS DELIBERATELY SMALL. Almost nothing here is business logic — it is window and
// OS plumbing. The search itself lives in the Python daemon, and the interface lives in
// ui/index.html. **If someone wanted to review this app, this is not the file that matters**,
// which is the whole reason a Rust shell is acceptable in a project meant to be readable.
//
// ⚠️ THE THREE THINGS THAT MAKE IT FEEL LIKE SPOTLIGHT, in order of importance:
//    1. a GLOBAL HOTKEY  — summon it from anywhere, without finding an icon
//    2. a TRAY ICON      — always reachable, never in the dock
//    3. a FLOATING, FRAMELESS, ALWAYS-ON-TOP window that hides on Escape
//
// ⚠️ AND THE HOTKEY IS THE ONE MOST LIKELY TO FAIL. Qt has no cross-platform global-hotkey API
// at all, and on macOS a global hotkey can require Accessibility permission. That is why this
// was probed before the UI was built around it.

use std::process::{Child, Command};
use std::sync::Mutex;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager, WebviewWindow,
};
use tauri_plugin_global_shortcut::ShortcutState;

/// ⚠️ NOT Cmd+Space — that is Spotlight's, and **overriding a well-known system shortcut in an
/// open-source app is a hostile default.** Cmd+Shift+Space is unclaimed and adjacent to the
/// muscle memory people already have.
const HOTKEY: &str = "CmdOrCtrl+Shift+Space";

/// Handle to the Python daemon, if this process started it.
/// ⚠️ Held so it can be killed on exit. Orphaned daemons holding a port are the classic
/// "it worked once, now it says address in use" bug and the user has no way to diagnose it.
struct Daemon(Mutex<Option<Child>>);

/// ⚠️ WHERE THE PYTHON LIVES IS CONFIGURATION, NOT A CONSTANT. In development the venv is a
/// sibling directory; in a packaged app it would be a bundled sidecar. Guessing one path is how
/// an app works on the author's machine and nowhere else.
// ⚠️ RETURNS None WHEN NO VENV EXISTS, AND THAT IS THE FIX RATHER THAN A DETAIL.
//
// This list used to end with `"python3"` as a per-root fallback, and the caller does:
//
//     let py = roots.iter().find_map(|r| python_for(r))?;
//
// ⚠️ So the FIRST root always "succeeded", because `python3` always exists. The resource
// directory is tried before the repo root, and under `cargo run` it resolves to `target/debug/`
// — where there is no venv. The shell therefore launched THE SYSTEM PYTHON, which has no numpy,
// and the daemon died with:
//
//     ModuleNotFoundError: No module named 'numpy'
//
// ⚠️ THE LESSON IS ABOUT WHERE A FALLBACK LIVES. A fallback inside a loop over candidates makes
// every candidate look valid, so the loop can never reach the good one. **It belongs after the
// search, not inside it.**
fn python_for(repo: &str) -> Option<String> {
    for cand in [
        std::env::var("DEVICE_SEARCH_PYTHON").unwrap_or_default(),
        format!("{repo}/.venv/bin/python"),
        format!("{repo}/../ai-engineer-learning/.venv/bin/python"),
    ] {
        if !cand.is_empty() && std::path::Path::new(&cand).exists() {
            return Some(cand);
        }
    }
    None
}

/// ⚠️ CHECK, DON'T ASSUME. If a daemon is already running we must NOT start a second one — the
/// second one fails to bind, and the UI silently talks to the FIRST one, which may have been
/// started hours ago against a stale index. That is a genuinely confusing bug to chase.
fn daemon_is_up() -> bool {
    std::net::TcpStream::connect_timeout(
        &"127.0.0.1:8734".parse().unwrap(),
        std::time::Duration::from_millis(300),
    )
    .is_ok()
}

fn ensure_daemon(app: &tauri::AppHandle) -> Option<Child> {
    if daemon_is_up() {
        return None;
    }
    // ⚠️⚠️ TWO CANDIDATE ROOTS, BECAUSE THE CORRECT ONE DIFFERS BETWEEN DEV AND PACKAGED.
    //
    // My first version used ONLY `BaseDirectory::Resource`. That resolves to `target/debug/`
    // under `cargo run`, NOT to the repo — so the daemon never started and the app looked
    // broken while the code was correct. Measured: the shell launched, the tray appeared, and
    // every search failed with "daemon unreachable".
    //
    // ⚠️ `CARGO_MANIFEST_DIR` is baked in AT COMPILE TIME and points at `src-tauri/`, so its
    // parent is the repo. That is the right answer in development and a stale absolute path in
    // a shipped binary — which is exactly why the resource dir is tried too, first being the
    // one that matters for whatever mode we are actually running in.
    let mut roots: Vec<String> = Vec::new();
    if let Ok(p) = app.path().resolve(".", tauri::path::BaseDirectory::Resource) {
        if let Some(s) = p.to_str() {
            roots.push(s.to_string());
        }
    }
    let dev_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.to_str())
        .unwrap_or("")
        .to_string();
    if !dev_root.is_empty() {
        roots.push(dev_root);
    }

    // ⚠️ TRY EVERY ROOT FOR A REAL VENV *BEFORE* FALLING BACK. The fallback is deliberately
    // OUTSIDE the loop: inside it, it made the first root always match and the venv on any later
    // root could never be reached. That is how the shell ended up running system python, which
    // has no numpy, and dying with a ModuleNotFoundError while everything looked correct.
    let py = roots
        .iter()
        .find_map(|r| python_for(r))
        .or_else(|| Some("python3".to_string()))?;
    let repo = roots
        .iter()
        .find(|r| std::path::Path::new(&format!("{r}/device_search")).is_dir())
        .cloned()
        .unwrap_or_else(|| roots.first().cloned().unwrap_or_else(|| ".".into()));
    let child = Command::new(py)
        .args(["-m", "device_search.server", "--port", "8734"])
        .current_dir(&repo)
        .spawn()
        .ok()?;
    Some(child)
}

/// ⚠️ HIDE ON BLUR IS WHAT MAKES IT FEEL LIKE A SEARCH BOX RATHER THAN A WINDOW. Spotlight
/// disappears the moment you click elsewhere; without this the user has to dismiss it
/// manually, which is the difference between summoning a tool and managing a window.
fn toggle(window: &WebviewWindow) {
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        // ⚠️ CENTRE ON SHOW, not only on creation. The user may have moved displays since the
        // app started, and a window that reappears on a disconnected monitor looks like a crash.
        let _ = window.center();
        let _ = window.show();
        let _ = window.set_focus();
        // ⚠️ Tell the frontend to focus its input. The OS focus and the DOM focus are separate,
        // so showing the window does NOT put the cursor in the box.
        let _ = window.emit("focus-input", ());
    }
}

/// ⚠️ A DESKTOP RUST BINARY NEEDS `main`. `#[mobile_entry_point]` only marks `run` as the entry
/// for iOS/Android builds; on desktop it generates nothing. Omitting this produced
/// `error[E0601]: main function not found in crate` — after every crate had compiled, so it
/// looked like a toolchain problem and was a two-line omission.
fn main() {
    run();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ⚠️ THE SHORTCUT IS PARSED AT STARTUP, and a failure to parse must not be silent —
        // a dead hotkey with a working tray icon looks like "the feature was never built".
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcut(HOTKEY)
                .unwrap()
                .with_handler(|app, _shortcut, event| {
                    // ⚠️ FIRE ON PRESS ONLY. Without the state check the handler runs twice —
                    // once on press and once on release — so the window toggles on and straight
                    // back off, which reads as "the hotkey does nothing".
                    if event.state() == ShortcutState::Pressed {
                        if let Some(w) = app.get_webview_window("main") {
                            toggle(&w);
                        }
                    }
                })
                .build(),
        )
        .manage(Daemon(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let child = ensure_daemon(&handle);
            *app.state::<Daemon>().0.lock().unwrap() = child;

            // ---- tray -------------------------------------------------------
            // ⚠️ THE TRAY IS THE APP'S PRESENCE. With no dock icon and no window on screen,
            // the tray is the only thing telling the user it is running — and the only way to
            // quit it.
            let search = MenuItem::with_id(app, "search", "Search…  ⇧⌘Space", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&search, &quit])?;
            let _tray = TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("device-search")
                .menu(&menu)
                .show_menu_on_left_click(false)   // ⚠️ left click should SEARCH, not open a menu
                .on_menu_event(|app, ev| match ev.id().as_ref() {
                    "search" => {
                        if let Some(w) = app.get_webview_window("main") {
                            toggle(&w);
                        }
                    }
                    "quit" => {
                        // ⚠️ KILL THE DAEMON WE STARTED, or it outlives the app and holds the
                        // port. Only kill OUR child — a daemon the user started themselves is
                        // theirs, and killing it would be rude and confusing.
                        if let Some(mut c) = app.state::<Daemon>().0.lock().unwrap().take() {
                            let _ = c.kill();
                        }
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    // ⚠️ Left click toggles the window. macOS shows the menu on left click by
                    // default, which is why `show_menu_on_left_click(false)` is set above.
                    if let tauri::tray::TrayIconEvent::Click {
                        button: tauri::tray::MouseButton::Left,
                        button_state: tauri::tray::MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(w) = tray.app_handle().get_webview_window("main") {
                            toggle(&w);
                        }
                    }
                })
                .build(app)?;

            // ⚠️ HIDE, DO NOT CLOSE. Closing the window would exit the app on some platforms
            // and leave the tray icon orphaned on others. Escape must dismiss, not quit.
            if let Some(w) = app.get_webview_window("main") {
                let w2 = w.clone();
                w.on_window_event(move |ev| {
                    if let tauri::WindowEvent::Focused(false) = ev {
                        let _ = w2.hide();
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![read_token, daemon_port, open_path,
                                               hide_window])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// ⚠️ THE TOKEN IS READ IN RUST AND HANDED TO THE FRONTEND, never stored in the frontend.
///
/// The daemon writes it 0600 precisely so that other processes cannot read it. If the UI kept
/// it in localStorage it would be readable by anything that can run script in the webview, and
/// the 0600 protection on the file would be decorative.
#[tauri::command]
fn read_token() -> Result<String, String> {
    let home = std::env::var("HOME").map_err(|_| "no HOME".to_string())?;
    let path = format!("{home}/.device-search/api.token");
    std::fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("cannot read {path}: {e}"))
}

#[tauri::command]
fn daemon_port() -> u16 {
    8734
}

/// ⚠️ THE SHELL EXISTS PARTLY FOR THIS. A webview cannot launch a file manager or open a
/// document in the user's chosen application — the OS gives that power to native processes only.
///
/// ⚠️ THE PATH COMES FROM THE API AS AN ABSOLUTE PATH, never reconstructed from the path shown
/// in the UI. The display path has `~` substituted for the home directory, which is a nice thing
/// to look at and not a real filesystem location. Rebuilding it in the frontend is how a
/// "reveal in Finder" button ends up opening the wrong folder — or nothing.
#[tauri::command]
fn open_path(path: String, reveal: bool) -> Result<(), String> {
    if !std::path::Path::new(&path).exists() {
        return Err(format!("no longer exists: {path}"));
    }
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = Command::new("open");
        if reveal { c.arg("-R"); }
        c.arg(&path);
        c
    };
    #[cfg(target_os = "windows")]
    let mut cmd = {
        // ⚠️ `/select,` on Windows opens Explorer with the file highlighted, which is the
        // closest equivalent to macOS's `open -R`.
        let mut c = Command::new("explorer");
        if reveal { c.arg(format!("/select,{path}")); } else { c.arg(&path); }
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = {
        // ⚠️ Linux has no universal "reveal". Opening the PARENT DIRECTORY is the honest
        // fallback — it is where the file is, which is what reveal is for.
        let mut c = Command::new("xdg-open");
        if reveal {
            c.arg(std::path::Path::new(&path).parent().unwrap_or(std::path::Path::new("/")));
        } else {
            c.arg(&path);
        }
        c
    };
    cmd.spawn().map(|_| ()).map_err(|e| format!("cannot open: {e}"))
}

/// ⚠️ HIDE, NEVER CLOSE. Closing the last window exits the process on some platforms, and
/// leaving the tray icon pointing at nothing on others.
#[tauri::command]
fn hide_window(app: tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
}
