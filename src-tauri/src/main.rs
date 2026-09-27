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

/// ⚠️⚠️ WHY HIDE-ON-BLUR NEEDS AN EXCEPTION, AND WHY IT TOOK A BUG REPORT TO SEE IT.
///
/// The window hides whenever it loses focus — that is the behaviour that makes it feel like a
/// search box rather than a window, and it is worth keeping.
///
/// ⚠️ BUT A NATIVE FILE DIALOG TAKES FOCUS. So opening the folder picker made the main window
/// lose focus, the handler hid it, and the modal sheet went with its parent — **the chooser
/// appeared and vanished in the same instant**, which is exactly what the user reported.
///
/// ⚠️ The two features are each correct and only conflict when combined, which is why neither
/// code review nor either test would have found it. It needed someone to open the dialog.
static DIALOG_OPEN: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

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
    // ⚠️⚠️ THE DAEMON'S OUTPUT MUST GO SOMEWHERE. A spawned child inherits this process's
    // stdout, and when the app is launched from the Finder that is /dev/null — so a daemon that
    // crashes on startup dies IN SILENCE and the UI just says "did not start" with no reason.
    // Measured: the daemon genuinely failed to start and there was no way to find out why.
    let log_path = std::path::Path::new(&std::env::var("HOME").ok()?)
        .join(".device-search")
        .join("daemon.log");
    if let Some(dir) = log_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let out = std::fs::OpenOptions::new().create(true).append(true).open(&log_path).ok();
    let err = out.as_ref().and_then(|f| f.try_clone().ok());

    let mut cmd = Command::new(py);
    // ⚠️ `-u` IS NOT OPTIONAL FOR A LOGGED CHILD. Python buffers stdout when it is not a
    // terminal, so the daemon's startup messages sat in a 8KB buffer and never reached the file —
    // meaning a crash before the buffer filled would produce an EMPTY log and a user staring at
    // "did not start" with nothing to read. Unbuffered is the difference between a log that
    // works and a log that exists.
    cmd.args(["-u", "-m", "device_search.server", "--port", "8734"]).current_dir(&repo);
    if let Some(f) = out {
        cmd.stdout(std::process::Stdio::from(f));
    }
    if let Some(f) = err {
        cmd.stderr(std::process::Stdio::from(f));
    }
    Some(cmd.spawn().ok()?)
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
        .plugin(tauri_plugin_dialog::init())
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
            // ⚠️⚠️ A COLOURED 512px PNG IS THE WRONG ASSET FOR A MENU-BAR ICON.
            // macOS menu-bar items are TEMPLATE images — black plus alpha, ~22pt — which the
            // system inverts for light and dark mode. A large coloured icon is scaled down badly
            // and does not adapt, so the item can exist and be effectively invisible.
            // ⚠️ AND A MISSING FILE MUST COST A NICE ICON, NOT THE TRAY ITEM. `.unwrap()` here
            // would panic inside setup() and take the whole tray with it.
            let tray_icon = tauri::image::Image::from_path(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("icons/tray.png"))
                .ok()
                .or_else(|| app.default_window_icon().cloned());
            let tray = TrayIconBuilder::with_id("main")
                .icon(tray_icon.unwrap_or_else(|| tauri::image::Image::new_owned(
                    vec![0, 0, 0, 0], 1, 1)))
                .icon_as_template(true)
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
            // ⚠️ THE HANDLE MUST OUTLIVE SETUP OR THE ICON IS REMOVED. `manage` stores it in the
            // app's state, which lives as long as the app does.
            app.manage(tray);

            // ⚠️ HIDE, DO NOT CLOSE. Closing the window would exit the app on some platforms
            // and leave the tray icon orphaned on others. Escape must dismiss, not quit.
            if let Some(w) = app.get_webview_window("main") {
                let w2 = w.clone();
                w.on_window_event(move |ev| {
                    if let tauri::WindowEvent::Focused(false) = ev {
                        // ⚠️ UNLESS A DIALOG IS UP. Hiding the parent of a modal dialog closes the
                        // dialog too, so focus loss caused by our own picker must not hide anything.
                        if !DIALOG_OPEN.load(std::sync::atomic::Ordering::SeqCst) {
                            let _ = w2.hide();
                        }
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![read_token, daemon_port, open_path,
                                               hide_window, pick_folder])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        // ⚠️⚠️ `RunEvent::Reopen` IS THE macOS "USER CLICKED THE DOCK ICON" EVENT.
        //
        // Without this handler the window is a ONE-WAY DOOR. It hides on blur — which is exactly
        // what makes it feel like a search box rather than a window — and clicking the dock icon
        // does nothing, so getting it back requires quitting and relaunching. That was the
        // reported behaviour.
        //
        // ⚠️ THE FIX IS NOT TO STOP HIDING ON BLUR. Hide-on-blur is the behaviour worth keeping.
        // The fix is to make EVERY route back to the window work: dock, tray, and hotkey.
        .run(|app, event| {
            if let tauri::RunEvent::Reopen { .. } = event {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.center();
                    let _ = w.show();
                    let _ = w.set_focus();
                    let _ = w.emit("focus-input", ());
                }
            }
        });
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

/// ⚠️⚠️ THE NATIVE FOLDER PICKER, AND WHY IT IS NOT A TEXT INPUT.
///
/// The setup screen asked the user to TYPE a path. That requires them to know where the folder
/// is, spell it exactly, and know whether macOS calls it `~/Documents` or `~/documents` — and
/// when they get it wrong the answer is "not a directory", which is the tool blaming them for
/// not knowing something it could have just shown them.
///
/// ⚠️ IT CAN PICK MORE THAN ONE FOLDER, in one dialog. Three trips through the picker to choose
/// Documents, Desktop and Downloads is three chances to give up.
///
/// ⚠️ AND IT USES A CHANNEL RATHER THAN `blocking_pick_folder`. The blocking variant waits on
/// the MAIN THREAD, and on macOS the file dialog is ALSO on the main thread — so waiting there
/// is how a native picker deadlocks the entire app. The callback returns, the channel carries
/// the answer, and the command thread does the waiting.
#[tauri::command]
async fn pick_folder(app: tauri::AppHandle, multiple: bool) -> Vec<String> {
    use tauri_plugin_dialog::DialogExt;
    // ⚠️ SET BEFORE THE DIALOG OPENS, CLEARED AFTER IT RETURNS, and cleared even on the timeout
    // path — a flag left set would disable hide-on-blur for the rest of the session, which looks
    // like the feature quietly stopped working.
    DIALOG_OPEN.store(true, std::sync::atomic::Ordering::SeqCst);
    let (tx, rx) = std::sync::mpsc::channel();
    let mut dlg = app.dialog().file();
    // ⚠️ PARENTED TO THE WINDOW. An unparented dialog on macOS is a separate window that can
    // appear behind the app, which is a different way to look like it never opened.
    if let Some(win) = app.get_webview_window("main") {
        dlg = dlg.set_parent(&win);
    }
    if multiple {
        dlg.pick_folders(move |paths| {
            let _ = tx.send(paths.unwrap_or_default()
                .into_iter().map(|p| p.to_string()).collect::<Vec<_>>());
        });
    } else {
        dlg.pick_folder(move |path| {
            let _ = tx.send(path.map(|p| vec![p.to_string()]).unwrap_or_default());
        });
    }
    // ⚠️ A TIMEOUT, because a cancelled dialog that never fires its callback would otherwise
    // leave the command hanging for the lifetime of the app.
    let got = rx.recv_timeout(std::time::Duration::from_secs(600)).unwrap_or_default();
    DIALOG_OPEN.store(false, std::sync::atomic::Ordering::SeqCst);
    // ⚠️ THE WINDOW IS SHOWN AGAIN AFTERWARDS. It never hid (the flag prevented it), but the
    // dialog took focus, so without this the search box is left behind whatever the user is
    // looking at — and typing goes nowhere.
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.set_focus();
        let _ = win.emit("focus-input", ());
    }
    got
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
