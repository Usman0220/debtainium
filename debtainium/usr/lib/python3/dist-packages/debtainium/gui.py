"""
Debtainium GTK4 + Libadwaita Graphical Interface (Verbose & Enhanced)
Provides a rich, detailed desktop interface with interactive expander rows,
live real-time activity log drawer, repository inspector preview, status summary,
and full package lifecycle management.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from debtainium.core import (
    APP_VERSION,
    AppEntry,
    ChecksumMismatchError,
    DebtainiumError,
    InstallationError,
    clean_version,
    compare_versions,
    download_asset_file,
    export_apps,
    extract_sha256_digest,
    fetch_latest_release,
    get_cache_dir,
    get_installed_version,
    get_system_arch,
    inspect_deb,
    install_deb_package,
    is_update_available,
    load_apps,
    merge_imported_apps,
    normalize_repo,
    pick_release_asset,
    save_apps,
    uninstall_deb_package,
    verify_file_sha256,
)


class DebtainiumWindow(Adw.ApplicationWindow):
    """Main application window with verbose inspector and activity console."""

    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Debtainium")
        self.set_default_size(860, 680)

        self.apps: List[AppEntry] = []
        self.is_busy = False
        self.search_filter_text = ""

        # Toast overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # ToolbarView layout
        self.toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(self.toolbar_view)

        # HeaderBar
        self.header_bar = Adw.HeaderBar()
        title_widget = Adw.WindowTitle(
            title="Debtainium",
            subtitle=f"GitHub .deb Manager • v{APP_VERSION} • {get_system_arch()}"
        )
        self.header_bar.set_title_widget(title_widget)

        # Start Header Buttons
        self.add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.add_btn.set_tooltip_text("Add Application from GitHub")
        self.add_btn.connect("clicked", self.on_add_clicked)
        self.header_bar.pack_start(self.add_btn)

        # End Header Buttons
        self.update_all_btn = Gtk.Button(label="Update All")
        self.update_all_btn.set_tooltip_text("Install every available update")
        self.update_all_btn.add_css_class("suggested-action")
        self.update_all_btn.connect("clicked", self.on_update_all_clicked)
        self.header_bar.pack_end(self.update_all_btn)

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("Check All Tracked Apps for Updates")
        self.refresh_btn.connect("clicked", self.on_check_all_clicked)
        self.header_bar.pack_end(self.refresh_btn)

        self.log_toggle_btn = Gtk.ToggleButton.new()
        self.log_toggle_btn.set_icon_name("utilities-terminal-symbolic")
        self.log_toggle_btn.set_tooltip_text("Toggle Real-Time Activity Log")
        self.log_toggle_btn.set_active(True)
        self.log_toggle_btn.connect("toggled", self.on_toggle_log)
        self.header_bar.pack_end(self.log_toggle_btn)

        # Main menu: background check scheduling + about
        menu = Gio.Menu()
        menu.append("Import tracked apps…", "win.import_apps")
        menu.append("Export tracked apps…", "win.export_apps")
        menu.append("Enable 6-hourly background checks", "win.enable_timer")
        menu.append("Disable background checks", "win.disable_timer")
        menu.append("About Debtainium", "win.about")
        self.menu_btn = Gtk.MenuButton()
        self.menu_btn.set_icon_name("open-menu-symbolic")
        self.menu_btn.set_tooltip_text("Main Menu")
        self.menu_btn.set_menu_model(menu)
        self.header_bar.pack_end(self.menu_btn)

        for name, handler in (
            ("import_apps", self.on_import_apps),
            ("export_apps", self.on_export_apps),
            ("enable_timer", self.on_enable_timer),
            ("disable_timer", self.on_disable_timer),
            ("about", self.on_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.toolbar_view.add_top_bar(self.header_bar)

        # Main vertical content split
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.toolbar_view.set_content(self.main_box)

        # Progress reporting banner
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.progress_box.set_margin_start(16)
        self.progress_box.set_margin_end(16)
        self.progress_box.set_margin_top(8)
        self.progress_box.set_margin_bottom(4)
        self.progress_box.set_visible(False)

        self.progress_label = Gtk.Label(label="Working...")
        self.progress_label.set_halign(Gtk.Align.START)
        self.progress_label.add_css_class("caption")
        self.progress_box.append(self.progress_label)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(False)
        self.progress_box.append(self.progress_bar)
        self.main_box.append(self.progress_box)

        # Dashboard Summary Bar
        self.summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.summary_box.set_margin_start(16)
        self.summary_box.set_margin_end(16)
        self.summary_box.set_margin_top(10)
        self.summary_box.set_margin_bottom(6)
        self.summary_box.set_halign(Gtk.Align.FILL)

        self.total_chip = self.create_summary_chip("📦 Total Tracked", "0")
        self.updates_chip = self.create_summary_chip("🔄 Updates Available", "0", accent=True)
        self.uptodate_chip = self.create_summary_chip("✅ Up to Date", "0", success=True)
        self.arch_chip = self.create_summary_chip("💻 System Arch", get_system_arch())

        self.summary_box.append(self.total_chip["box"])
        self.summary_box.append(self.updates_chip["box"])
        self.summary_box.append(self.uptodate_chip["box"])
        self.summary_box.append(self.arch_chip["box"])
        self.main_box.append(self.summary_box)

        # Search Bar
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Filter by name, repo, package name, or status...")
        self.search_entry.set_margin_start(16)
        self.search_entry.set_margin_end(16)
        self.search_entry.set_margin_bottom(8)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.main_box.append(self.search_entry)

        # Paned container: Apps list on top, Activity Log drawer on bottom
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.paned.set_vexpand(True)
        self.paned.set_hexpand(True)
        self.main_box.append(self.paned)

        # Top pane: Apps ScrolledWindow
        self.scrolled_apps = Gtk.ScrolledWindow()
        self.scrolled_apps.set_vexpand(True)
        self.scrolled_apps.set_hexpand(True)
        self.paned.set_start_child(self.scrolled_apps)

        self.clamp = Adw.Clamp(maximum_size=820, tightening_threshold=650)
        self.clamp.set_margin_top(12)
        self.clamp.set_margin_bottom(12)
        self.clamp.set_margin_start(16)
        self.clamp.set_margin_end(16)
        self.scrolled_apps.set_child(self.clamp)

        self.list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.clamp.set_child(self.list_container)

        # Empty state page
        self.empty_page = Adw.StatusPage(
            icon_name="package-x-generic-symbolic",
            title="No Applications Tracked",
            description="Add third-party software from GitHub Releases to monitor, verify, and update .deb packages."
        )
        add_empty_btn = Gtk.Button(label="Add Your First Application")
        add_empty_btn.add_css_class("suggested-action")
        add_empty_btn.add_css_class("pill")
        add_empty_btn.set_halign(Gtk.Align.CENTER)
        add_empty_btn.connect("clicked", self.on_add_clicked)
        self.empty_page.set_child(add_empty_btn)
        self.list_container.append(self.empty_page)

        # Apps ListBox
        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("boxed-list")
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_container.append(self.list_box)

        # Bottom pane: Real-time Activity Log Drawer
        self.log_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.log_frame.set_size_request(-1, 140)
        self.paned.set_end_child(self.log_frame)

        # Log Header
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        log_header.set_margin_start(12)
        log_header.set_margin_end(12)
        log_header.set_margin_top(4)
        log_header.set_margin_bottom(4)

        log_title = Gtk.Label(label="<b>Live Activity Log</b>")
        log_title.set_use_markup(True)
        log_title.set_halign(Gtk.Align.START)
        log_header.append(log_title)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        log_header.append(spacer)

        clear_btn = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic")
        clear_btn.set_tooltip_text("Clear Log Output")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", lambda _b: self.clear_log())
        log_header.append(clear_btn)

        self.log_frame.append(log_header)

        # Log Scrolled Text View
        self.log_scrolled = Gtk.ScrolledWindow()
        self.log_scrolled.set_vexpand(True)
        self.log_scrolled.set_hexpand(True)
        self.log_frame.append(self.log_scrolled)

        self.log_text_view = Gtk.TextView()
        self.log_text_view.set_editable(False)
        self.log_text_view.set_cursor_visible(False)
        self.log_text_view.set_monospace(True)
        self.log_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_text_view.set_left_margin(12)
        self.log_text_view.set_right_margin(12)
        self.log_text_view.set_top_margin(6)
        self.log_text_view.set_bottom_margin(6)
        self.log_buffer = self.log_text_view.get_buffer()
        self.log_scrolled.set_child(self.log_text_view)

        # Initial log entry
        self.log(f"Debtainium {APP_VERSION} initialized on {get_system_arch()} platform.")
        self.load_data()

    def create_summary_chip(self, title: str, value: str, accent: bool = False, success: bool = False) -> dict:
        """Create a metric card chip for the top dashboard."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_hexpand(True)
        box.add_css_class("card")
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        inner.set_margin_start(10)
        inner.set_margin_end(10)
        inner.set_margin_top(6)
        inner.set_margin_bottom(6)
        box.append(inner)

        lbl_title = Gtk.Label(label=title)
        lbl_title.set_halign(Gtk.Align.START)
        lbl_title.add_css_class("caption")
        lbl_title.add_css_class("dim-label")
        inner.append(lbl_title)

        lbl_val = Gtk.Label(label=value)
        lbl_val.set_halign(Gtk.Align.START)
        lbl_val.add_css_class("title-3")
        if accent:
            lbl_val.add_css_class("accent")
        elif success:
            lbl_val.add_css_class("success")
        inner.append(lbl_val)

        return {"box": box, "value": lbl_val}

    def log(self, message: str):
        """Append a timestamped message to the live activity console."""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}\n"

        def _append():
            end_iter = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end_iter, formatted)
            # Auto scroll to bottom
            mark = self.log_buffer.create_mark(None, end_iter, False)
            self.log_text_view.scroll_to_mark(mark, 0.05, False, 0.0, 1.0)

        GLib.idle_add(_append)

    def clear_log(self):
        """Clear the activity log."""
        self.log_buffer.set_text("")
        self.log("Activity log cleared.")

    def on_toggle_log(self, btn):
        """Toggle activity log visibility."""
        visible = btn.get_active()
        self.log_frame.set_visible(visible)

    def _systemctl_user(self, *args: str) -> Tuple[bool, str]:
        """Run `systemctl --user ...` and return (ok, combined output)."""
        try:
            proc = subprocess.run(
                ["systemctl", "--user", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, out.strip()
        except FileNotFoundError:
            return False, "systemctl not found"
        except subprocess.TimeoutExpired:
            return False, "systemctl timed out"

    def on_export_apps(self, _action, _param):
        """Save the tracked-app list to a JSON file the user picks."""
        if not self.apps:
            self.show_toast("Nothing to export yet.")
            return
        dialog = Gtk.FileDialog(title="Export tracked applications")
        dialog.set_initial_name("debtainium-apps.json")
        dialog.save(self, None, self._export_finish)

    def _export_finish(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error as e:
            self.log(f"Export cancelled: {e.message}")
            return
        path = Path(gfile.get_path())
        try:
            payload = export_apps(self.apps)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            self.log(f"ERROR: export failed: {e}")
            self.show_toast("Export failed — see the log.", timeout=6)
            return
        self.log(f"Exported {len(self.apps)} application(s) to {path}")
        self.show_toast(f"Exported {len(self.apps)} app(s).")

    def on_import_apps(self, _action, _param):
        """Merge a JSON export back into the tracked list."""
        dialog = Gtk.FileDialog(title="Import tracked applications")
        json_filter = Gtk.FileFilter()
        json_filter.set_name("JSON files")
        json_filter.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(json_filter)
        dialog.set_filters(store)
        dialog.set_default_filter(json_filter)
        dialog.open(self, None, self._import_finish)

    def _import_finish(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error as e:
            self.log(f"Import cancelled: {e.message}")
            return
        path = Path(gfile.get_path())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            self.log(f"ERROR: could not read {path}: {e}")
            self.show_toast("Import failed — cannot read file.", timeout=6)
            return
        except json.JSONDecodeError as e:
            self.log(f"ERROR: {path} is not valid JSON: {e}")
            self.show_toast("Import failed — invalid JSON.", timeout=6)
            return

        entries = data.get("apps") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            self.log(f"ERROR: {path} has no 'apps' list.")
            self.show_toast("Import failed — no 'apps' list.", timeout=6)
            return

        imported: List[AppEntry] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                repo = normalize_repo(str(item.get("repo", "")))
            except ValueError as e:
                self.log(f"Skipping import entry: {e}")
                continue
            clean = dict(item)
            clean["repo"] = repo
            imported.append(AppEntry.from_dict(clean))

        if not imported:
            self.show_toast("Import file contained no usable entries.", timeout=6)
            return

        merged, added, updated = merge_imported_apps(self.apps, imported)
        self.apps = merged
        save_apps(self.apps)
        self.refresh_list_ui()
        self.log(f"Imported {path.name}: {added} added, {updated} updated, "
                 f"{len(self.apps)} tracked in total.")
        self.show_toast(f"Imported: {added} added, {updated} updated.")

    def on_enable_timer(self, _action, _param):
        """Enable and start the background update-check timer."""
        ok, out = self._systemctl_user("enable", "--now", "debtainium-notify.timer")
        if ok:
            self.log("Enabled debtainium-notify.timer (checks every 6 hours).")
            self.show_toast("Background checks enabled (every 6 hours).")
        else:
            self.log(f"ERROR: could not enable timer: {out}")
            self.show_toast("Could not enable background checks.", timeout=6)

    def on_disable_timer(self, _action, _param):
        """Disable and stop the background update-check timer."""
        ok, out = self._systemctl_user("disable", "--now", "debtainium-notify.timer")
        if ok:
            self.log("Disabled debtainium-notify.timer.")
            self.show_toast("Background checks disabled.")
        else:
            self.log(f"ERROR: could not disable timer: {out}")
            self.show_toast("Could not disable background checks.", timeout=6)

    def on_about(self, _action, _param):
        """Show the About dialog."""
        about = Adw.AboutWindow(transient_for=self, modal=True)
        about.set_application_name("Debtainium")
        about.set_application_icon("debtainium")
        about.set_version(APP_VERSION)
        about.set_developer_name("Debtainium Developers")
        about.set_website("https://github.com/Usman0220/debtainium")
        about.set_issue_url("https://github.com/Usman0220/debtainium/issues")
        about.set_license_type(Gtk.License.MIT_X11)
        about.set_comments(
            "Tracks third-party software published as .deb assets on GitHub "
            "Releases, verifies SHA-256 digests, and installs updates through "
            "polkit and apt."
        )
        about.present()

    def show_toast(self, message: str, timeout: int = 4):
        """Display an Adw.Toast overlay."""
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def set_progress(self, visible: bool, fraction: float = 0.0, status_text: str = ""):
        """Update progress bar visibility, fraction, and message label."""
        self.progress_box.set_visible(visible)
        self.progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
        if status_text:
            self.progress_label.set_text(status_text)

    def on_search_changed(self, entry):
        """Filter the displayed list based on the search term."""
        self.search_filter_text = entry.get_text().strip().lower()
        self.refresh_list_ui()

    def load_data(self):
        """Reload apps from storage and update dashboard metrics."""
        self.apps = load_apps()
        self.log(f"Loaded {len(self.apps)} application(s) from ~/.config/debtainium/apps.json")
        self.refresh_list_ui()

    def refresh_dashboard_metrics(self):
        """Update summary cards at the top."""
        total = len(self.apps)
        updates = 0
        uptodate = 0

        for a in self.apps:
            inst = get_installed_version(a.deb_pkg)
            if not inst:
                continue
            if not a.latest_version:
                continue
            if compare_versions(inst, "lt", a.latest_version):
                updates += 1
            else:
                uptodate += 1

        self.total_chip["value"].set_text(str(total))
        self.updates_chip["value"].set_text(str(updates))
        self.uptodate_chip["value"].set_text(str(uptodate))

    def refresh_list_ui(self):
        """Render tracked applications into the ListBox using Adw.ExpanderRow."""
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

        self.refresh_dashboard_metrics()

        if not self.apps:
            self.empty_page.set_visible(True)
            self.list_box.set_visible(False)
            return

        self.empty_page.set_visible(False)
        self.list_box.set_visible(True)

        visible_count = 0
        for app in self.apps:
            # Check filter: every whitespace-separated term must appear somewhere
            # in the row's searchable text (name, repo, package, status, auto).
            if self.search_filter_text:
                inst = get_installed_version(app.deb_pkg) or ""
                if not inst:
                    status = "not installed"
                elif is_update_available(inst, app.latest_version):
                    status = "update available"
                else:
                    status = "up to date"
                haystack = " ".join((
                    app.name,
                    app.repo,
                    app.deb_pkg,
                    status,
                    "auto" if app.auto_update else "",
                )).lower()
                if not all(term in haystack for term in self.search_filter_text.split()):
                    continue

            visible_count += 1
            row = self.create_verbose_app_row(app)
            self.list_box.append(row)

    def create_verbose_app_row(self, app: AppEntry) -> Adw.ExpanderRow:
        """Build an interactive, verbose Adw.ExpanderRow with full technical inspection."""
        inst = get_installed_version(app.deb_pkg) or ""
        app.installed_version = inst
        latest = app.latest_version or "-"

        has_update = is_update_available(inst, app.latest_version)

        # Main Expander Row
        expander = Adw.ExpanderRow()
        expander.set_title(app.name)

        subtitle_text = f"<b>{app.repo}</b>  •  Installed: {inst or 'None'}  •  Latest: {latest}"
        expander.set_subtitle(subtitle_text)

        # Prefix package icon
        icon = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
        icon.set_icon_size(Gtk.IconSize.LARGE)
        expander.add_prefix(icon)

        # Status badge
        badge = Gtk.Label()
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_margin_end(6)

        if not inst:
            badge.set_markup("<span foreground='#888888' font_weight='bold'>Not Installed</span>")
            action_btn_label = "Install"
            action_btn_css = "suggested-action"
        elif has_update:
            badge.set_markup("<span foreground='#3584e4' font_weight='bold'>Update Available</span>")
            action_btn_label = "Update"
            action_btn_css = "suggested-action"
        else:
            badge.set_markup("<span foreground='#2ec27e' font_weight='bold'>Up to Date</span>")
            action_btn_label = "Reinstall"
            action_btn_css = "flat"

        expander.add_action(badge)

        # Primary Action Button
        action_btn = Gtk.Button(label=action_btn_label)
        action_btn.add_css_class(action_btn_css)
        action_btn.set_valign(Gtk.Align.CENTER)
        action_btn.connect("clicked", lambda _b, a=app: self.on_update_app(a))
        expander.add_action(action_btn)

        # Quick refresh button
        check_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.set_tooltip_text("Check for updates")
        check_btn.add_css_class("flat")
        check_btn.connect("clicked", lambda _b, a=app: self.on_check_single_app(a))
        expander.add_action(check_btn)

        # --- SUB-ROWS (Verbose Inspection Details) ---

        # 1. Repository & Upstream Link
        repo_row = Adw.ActionRow()
        repo_row.set_title("GitHub Upstream")
        repo_row.set_subtitle(f"https://github.com/{app.repo}")
        open_gh_btn = Gtk.Button.new_from_icon_name("web-browser-symbolic")
        open_gh_btn.set_tooltip_text("Open Repository in Web Browser")
        open_gh_btn.set_valign(Gtk.Align.CENTER)
        open_gh_btn.add_css_class("flat")
        open_gh_btn.connect("clicked", lambda _b, r=app.repo: self.open_url(f"https://github.com/{r}"))
        repo_row.add_suffix(open_gh_btn)
        expander.add_row(repo_row)

        # 2. Package Management & dpkg details
        pkg_row = Adw.ActionRow()
        pkg_row.set_title("Debian Package Identifier")
        pkg_row.set_subtitle(
            "Package: <tt>" + GLib.markup_escape_text(app.deb_pkg, -1)
            + "</tt>  •  Target Arch: <tt>" + GLib.markup_escape_text(get_system_arch(), -1)
            + "</tt>"
        )
        expander.add_row(pkg_row)

        # 3. Release & Version tracking
        ver_row = Adw.ActionRow()
        ver_row.set_title("Version Status")
        last_checked = app.last_checked if app.last_checked else "Never"
        ver_row.set_subtitle(f"Installed: <b>{inst or 'None'}</b>  •  Latest Release: <b>{latest}</b>  •  Last Checked: {last_checked}")
        expander.add_row(ver_row)

        # 4. Filter & Matching
        pattern_row = Adw.ActionRow()
        pattern_row.set_title("Asset Match Filter")
        patt_desc = (
            "Regex: <tt>" + GLib.markup_escape_text(app.asset_pattern, -1) + "</tt>"
            if app.asset_pattern
            else "Standard automatic architecture selector"
        )
        pattern_row.set_subtitle(patt_desc)
        expander.add_row(pattern_row)

        # 5. Auto-update policy
        auto_row = Adw.ActionRow()
        auto_row.set_title("Automatic Installation")
        auto_row.set_subtitle(
            "Install new releases without asking when "
            "<tt>debtainium update --auto</tt> runs"
        )
        auto_switch = Gtk.Switch()
        auto_switch.set_valign(Gtk.Align.CENTER)
        auto_switch.set_active(bool(app.auto_update))
        auto_switch.connect("state-set", self.on_auto_update_toggled, app)
        auto_row.add_suffix(auto_switch)
        auto_row.set_activatable_widget(auto_switch)
        expander.add_row(auto_row)

        # 6. Action Bar Inside Expander
        actions_row = Adw.ActionRow()
        actions_row.set_title("Application Operations")

        bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar_box.set_valign(Gtk.Align.CENTER)

        # View Release Notes Button
        notes_btn = Gtk.Button(label="Release Notes")
        notes_btn.set_tooltip_text("Show the latest release notes")
        notes_btn.add_css_class("flat")
        notes_btn.connect("clicked", lambda _b, a=app: ReleaseNotesDialog(self, a).present())
        bar_box.append(notes_btn)

        # Uninstall Button (if package is installed)
        if inst:
            uninstall_btn = Gtk.Button(label="Uninstall Package")
            uninstall_btn.set_tooltip_text("Remove .deb from Debian system via pkexec apt-get remove")
            uninstall_btn.add_css_class("destructive-action")
            uninstall_btn.connect("clicked", lambda _b, a=app: self.on_uninstall_package(a))
            bar_box.append(uninstall_btn)

        # Untrack Button
        untrack_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        untrack_btn.set_tooltip_text("Remove from Debtainium tracking list")
        untrack_btn.add_css_class("flat")
        untrack_btn.connect("clicked", lambda _b, a=app: self.on_delete_app(a))
        bar_box.append(untrack_btn)

        actions_row.add_suffix(bar_box)
        expander.add_row(actions_row)

        return expander

    def open_url(self, url: str):
        """Open web URL in user's default browser."""
        self.log(f"Opening browser: {url}")
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception as e:
            subprocess.Popen(["xdg-open", url])

    def on_add_clicked(self, _btn):
        """Open enhanced Add Application dialog."""
        dialog = VerboseAddAppDialog(self)
        dialog.present()

    def on_delete_app(self, app: AppEntry):
        """Remove application from tracking."""
        self.apps = [a for a in self.apps if a.repo != app.repo]
        save_apps(self.apps)
        self.log(f"Removed '{app.name}' ({app.repo}) from tracking list.")
        self.refresh_list_ui()
        self.show_toast(f"Removed '{app.name}' from Debtainium.")

    def on_uninstall_package(self, app: AppEntry):
        """Uninstall Debian package using pkexec apt-get remove -y."""
        if self.is_busy:
            return
        self.is_busy = True
        self.set_progress(True, 0.5, f"Uninstalling {app.deb_pkg} via APT...")
        self.log(f"Initiating package removal for '{app.deb_pkg}' via pkexec apt-get remove...")

        def worker():
            try:
                uninstall_deb_package(app.deb_pkg)
                self.log(f"Successfully uninstalled package '{app.deb_pkg}' from system.")
                app.installed_version = ""
                save_apps(self.apps)

                def success_ui():
                    self.set_progress(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    self.show_toast(f"Uninstalled '{app.name}' successfully.")

                GLib.idle_add(success_ui)
            except Exception as e:
                self.log(f"ERROR: Failed to uninstall '{app.deb_pkg}': {e}")

                def err_ui():
                    self.set_progress(False)
                    self.is_busy = False
                    self.show_toast(f"Uninstall failed: {e}", timeout=6)

                GLib.idle_add(err_ui)

        threading.Thread(target=worker, daemon=True).start()

    def on_auto_update_toggled(self, _switch, state, app: AppEntry):
        """Persist a per-app auto-update change made from the expander row."""
        app.auto_update = bool(state)
        save_apps(self.apps)
        if state:
            self.log(f"Auto-update enabled for {app.name} ({app.repo}).")
            self.show_toast(f"{app.name} will be installed automatically.")
        else:
            self.log(f"Auto-update disabled for {app.name} ({app.repo}).")
            self.show_toast(f"{app.name} now requires manual updates.")
        return False  # let Gtk update the switch itself

    def on_check_single_app(self, app: AppEntry):
        """Check a single app for updates in a background worker."""
        if self.is_busy:
            return
        self.is_busy = True
        self.set_progress(True, 0.3, f"Checking GitHub for {app.name}...")
        self.log(f"Querying GitHub Releases API: https://api.github.com/repos/{app.repo}/releases/latest...")

        def worker():
            try:
                release = fetch_latest_release(app.repo)
                tag = release.get("tag_name", "")
                latest = clean_version(tag)
                self.log(f"Received release metadata for {app.repo}: tag '{tag}' (normalized: {latest})")

                arch = get_system_arch()
                asset = pick_release_asset(release, arch=arch, pattern=app.asset_pattern)
                if asset:
                    self.log(f"Architecture match for '{arch}': asset '{asset.get('name')}' ({asset.get('size', 0) // 1024} KB)")
                else:
                    self.log(f"WARNING: No matching .deb asset found for architecture '{arch}'!")

                app.latest_version = latest
                app.last_checked = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                inst = get_installed_version(app.deb_pkg) or ""
                app.installed_version = inst
                save_apps(self.apps)

                has_upd = is_update_available(inst, latest)
                if has_upd:
                    self.log(f"Update evaluation: installed='{inst}' < latest='{latest}' --> UPDATE AVAILABLE")
                else:
                    self.log(f"Update evaluation: {app.name} is up to date ({inst or latest})")

                def update_ui():
                    self.set_progress(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    if has_upd:
                        self.show_toast(f"Update available for {app.name}: {latest}")
                    else:
                        self.show_toast(f"{app.name} is up to date ({inst or latest})")

                GLib.idle_add(update_ui)
            except Exception as e:
                self.log(f"ERROR: Failed to check {app.name}: {e}")

                def on_err():
                    self.set_progress(False)
                    self.is_busy = False
                    self.show_toast(f"Error checking {app.name}: {e}", timeout=5)

                GLib.idle_add(on_err)

        threading.Thread(target=worker, daemon=True).start()

    def on_check_all_clicked(self, _btn):
        """Check all apps for updates in a background thread."""
        if self.is_busy or not self.apps:
            return
        self.is_busy = True
        self.set_progress(True, 0.05, "Checking all tracked repositories...")
        self.log(f"Beginning batch update check for {len(self.apps)} application(s)...")

        def worker():
            total = len(self.apps)
            updates_count = 0
            for idx, app in enumerate(self.apps):
                try:
                    frac = (idx + 1) / total
                    GLib.idle_add(
                        lambda f=frac, a=app, i=idx, t=total: self.set_progress(
                            True, f, f"Checking {a.name} ({i + 1}/{t})..."
                        )
                    )
                    self.log(f"Checking [{idx + 1}/{total}] {app.name} ({app.repo})...")
                    release = fetch_latest_release(app.repo)
                    tag = release.get("tag_name", "")
                    latest = clean_version(tag)
                    app.latest_version = latest
                    app.last_checked = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    inst = get_installed_version(app.deb_pkg) or ""
                    app.installed_version = inst

                    if is_update_available(inst, latest):
                        updates_count += 1
                        self.log(f"  --> Update available: {inst or 'None'} -> {latest}")
                    else:
                        self.log(f"  --> Up to date ({inst or latest})")
                except Exception as e:
                    self.log(f"  --> Check failed: {e}")

            save_apps(self.apps)
            self.log(f"Batch check completed. {updates_count} update(s) ready for installation.")

            def done_ui():
                self.set_progress(False)
                self.refresh_list_ui()
                self.is_busy = False
                if updates_count > 0:
                    self.show_toast(f"Check complete: {updates_count} update(s) available!")
                else:
                    self.show_toast("All applications are up to date.")

            GLib.idle_add(done_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _install_latest(self, app: AppEntry) -> str:
        """
        Download, verify and install the newest release of `app`.

        Blocking; must run off the main loop. Returns the version now installed.
        Raises on any failure (nothing is swallowed here so batch callers can
        report per-app errors).
        """
        # 1. Fetch release
        self.log("Fetching latest release metadata from GitHub...")
        release = fetch_latest_release(app.repo)
        latest = clean_version(release.get("tag_name", ""))
        app.latest_version = latest

        # 2. Pick asset
        arch = get_system_arch()
        self.log(f"Selecting matching .deb asset for architecture '{arch}'...")
        asset = pick_release_asset(release, arch=arch, pattern=app.asset_pattern)
        if not asset:
            raise DebtainiumError(f"No .deb asset matching arch '{arch}' found in release {latest}")

        asset_name = asset["name"]
        download_url = asset["browser_download_url"]
        size_kb = asset.get("size", 0) // 1024

        self.log(f"Asset matched: '{asset_name}' ({size_kb} KB)")
        self.log(f"Download URL: {download_url}")

        # 3. Download
        repo_slug = app.repo.replace("/", "_")
        dest_file = get_cache_dir() / repo_slug / latest / asset_name

        if not dest_file.exists():
            self.log(f"Downloading to cache directory: {dest_file}...")

            def progress(downloaded: int, total: int):
                if total > 0:
                    fraction = max(0.1, min(0.85, downloaded / total))
                    pct = int(downloaded / total * 100)
                    status = f"Downloading {asset_name} ({pct}% • {downloaded // 1024}/{total // 1024} KB)..."
                    GLib.idle_add(lambda f=fraction, s=status: self.set_progress(True, f, s))

            download_asset_file(download_url, dest_file, progress)
            self.log("Asset download completed successfully.")
        else:
            self.log(f"Using existing cached asset: {dest_file}")

        # 4. SHA-256 verification
        self.log("Searching release for SHA-256 digest verification...")
        expected_hash = extract_sha256_digest(release, asset_name)
        if expected_hash:
            self.log(f"Found expected SHA-256 digest: {expected_hash}")
            GLib.idle_add(lambda: self.set_progress(True, 0.9, "Verifying SHA-256 checksum..."))
            if not verify_file_sha256(dest_file, expected_hash):
                dest_file.unlink(missing_ok=True)
                raise ChecksumMismatchError(f"SHA-256 digest verification failed for {asset_name}!")
            self.log("Cryptographic SHA-256 integrity verified successfully.")
        else:
            self.log("Notice: No SHA-256 digest provided in release; skipping checksum verification.")

        # 5. Inspect deb package
        info = inspect_deb(dest_file)
        if info.get("Package"):
            app.deb_pkg = info["Package"]
            self.log(f"Debian package name confirmed from deb control: '{app.deb_pkg}'")

        # 6. Install via pkexec apt-get
        self.log(f"Executing: pkexec apt-get install -y {dest_file}...")
        GLib.idle_add(lambda: self.set_progress(True, 0.95, "Installing package via pkexec (Polkit authorization)..."))
        GLib.idle_add(lambda: self.show_toast("Installing package via pkexec (check prompt)..."))

        install_deb_package(dest_file)
        self.log("APT package installation completed successfully.")

        # 7. Update status
        new_inst = get_installed_version(app.deb_pkg, use_cache=False) or latest
        app.installed_version = new_inst
        app.last_checked = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_apps(self.apps)
        self.log(f"Updated status for '{app.name}': Installed Version = {new_inst}")
        return new_inst

    def on_update_app(self, app: AppEntry):
        """Download, verify, and install app in a background worker."""
        if self.is_busy:
            return
        self.is_busy = True
        self.set_progress(True, 0.05, f"Fetching release for {app.name}...")
        self.log(f"=== Starting update/install workflow for {app.name} ({app.repo}) ===")

        def worker():
            try:
                new_inst = self._install_latest(app)

                def success_ui():
                    self.set_progress(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    self.show_toast(f"Successfully installed {app.name} ({new_inst})!", timeout=5)

                GLib.idle_add(success_ui)

            except Exception as e:
                self.log(f"ERROR: Installation failed: {e}")

                def err_ui():
                    self.set_progress(False)
                    self.is_busy = False
                    self.show_toast(f"Installation failed: {e}", timeout=6)

                GLib.idle_add(err_ui)

        threading.Thread(target=worker, daemon=True).start()

    def on_update_all_clicked(self, _btn):
        """Check every tracked app, then install each available update in turn."""
        if self.is_busy or not self.apps:
            return
        self.is_busy = True
        self.set_progress(True, 0.02, "Checking all applications for updates...")
        self.log(f"=== Update All: checking {len(self.apps)} application(s) ===")

        def worker():
            apps = list(self.apps)
            total = len(apps)
            pending: List[AppEntry] = []
            failed: List[str] = []

            # Phase 1 — find out what actually needs installing
            for idx, app in enumerate(apps):
                frac = 0.02 + 0.18 * ((idx + 1) / total)
                GLib.idle_add(
                    lambda f=frac, a=app, i=idx, t=total: self.set_progress(
                        True, f, f"Checking {a.name} ({i + 1}/{t})..."
                    )
                )
                try:
                    release = fetch_latest_release(app.repo)
                    latest = clean_version(release.get("tag_name", ""))
                    app.latest_version = latest
                    app.last_checked = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    inst = get_installed_version(app.deb_pkg) or ""
                    app.installed_version = inst
                    if is_update_available(inst, latest):
                        pending.append(app)
                        self.log(f"• {app.name}: {inst or 'not installed'} -> {latest}")
                except Exception as e:
                    failed.append(f"{app.name} (check: {e})")
                    self.log(f"• {app.name}: check failed — {e}")

            save_apps(self.apps)
            if not pending:
                self.log("Update All: nothing to install.")

                def nothing_ui():
                    self.set_progress(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    self.show_toast("All applications are up to date.")

                GLib.idle_add(nothing_ui)
                return

            # Phase 2 — install them one after another
            self.log(f"Update All: installing {len(pending)} update(s)...")
            installed = 0
            for pos, app in enumerate(pending, start=1):
                frac = 0.2 + 0.8 * ((pos - 1) / len(pending))
                GLib.idle_add(
                    lambda f=frac, a=app, i=pos, t=len(pending): self.set_progress(
                        True, f, f"Installing {a.name} ({i}/{t})..."
                    )
                )
                self.log(f"--- [{pos}/{len(pending)}] {app.name} ---")
                try:
                    new_inst = self._install_latest(app)
                    installed += 1
                    self.log(f"--- {app.name} installed ({new_inst}) ---")
                except Exception as e:
                    failed.append(f"{app.name} (install: {e})")
                    self.log(f"ERROR: {app.name} failed to install: {e}")

            save_apps(self.apps)

            def done_ui():
                self.set_progress(False)
                self.refresh_list_ui()
                self.is_busy = False
                summary = f"Update All: {installed} installed"
                if failed:
                    summary += f", {len(failed)} failed"
                    self.log("Update All failures: " + "; ".join(failed))
                self.show_toast(summary, timeout=6)

            GLib.idle_add(done_ui)

        threading.Thread(target=worker, daemon=True).start()


class ReleaseNotesDialog(Adw.Window):
    """Show the latest release's notes inside the app, fetched on demand."""

    def __init__(self, parent_window: DebtainiumWindow, app: AppEntry):
        super().__init__(
            transient_for=parent_window,
            modal=True,
            title=f"{app.name} — Release Notes",
        )
        self.parent_win = parent_window
        self.app_entry = app
        self.set_default_size(640, 520)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_title(True)
        toolbar_view.add_top_bar(header)

        # Placeholder while the network request is in flight
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar_view.set_content(self.stack)

        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_halign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        spinner_box.append(spinner)
        spinner_box.append(Gtk.Label(label="Fetching release notes…"))
        self.stack.add_named(spinner_box, "loading")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_top(12)
        scrolled.set_margin_bottom(12)
        scrolled.set_margin_start(16)
        scrolled.set_margin_end(16)

        self.body_view = Gtk.TextView()
        self.body_view.set_editable(False)
        self.body_view.set_cursor_visible(False)
        self.body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.body_view.set_left_margin(4)
        self.body_view.set_right_margin(4)
        # Keep the markdown verbatim: GitHub's rendering is not available here.
        self.body_view.add_css_class("monospace")
        scrolled.set_child(self.body_view)
        self.stack.add_named(scrolled, "notes")

        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        error_box.set_valign(Gtk.Align.CENTER)
        error_box.set_halign(Gtk.Align.CENTER)
        error_box.set_margin_start(24)
        error_box.set_margin_end(24)
        self.error_label = Gtk.Label()
        self.error_label.set_wrap(True)
        self.error_label.set_justify(Gtk.Justification.CENTER)
        error_box.append(self.error_label)
        open_btn = Gtk.Button(label="Open on GitHub")
        open_btn.add_css_class("pill")
        open_btn.set_halign(Gtk.Align.CENTER)
        open_btn.connect(
            "clicked",
            lambda _b: self.parent_win.open_url(
                f"https://github.com/{app.repo}/releases/latest"
            ),
        )
        error_box.append(open_btn)
        self.stack.add_named(error_box, "error")

        self.stack.set_visible_child_name("loading")
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            release = fetch_latest_release(self.app_entry.repo)
            tag = release.get("tag_name", "") or "(unknown tag)"
            name = release.get("name") or tag
            body = (release.get("body") or "").strip()
            published = (release.get("published_at") or "")[:10]
            header = f"{name}\n{tag}"
            if published:
                header += f"  •  {published}"
            text = header + "\n\n" + (body or "This release has no notes.")
            GLib.idle_add(self._show_text, text)
        except Exception as e:
            GLib.idle_add(self._show_error, str(e))

    def _show_text(self, text: str):
        self.body_view.get_buffer().set_text(text)
        self.stack.set_visible_child_name("notes")
        return False

    def _show_error(self, message: str):
        self.error_label.set_text(f"Could not fetch release notes:\n{message}")
        self.stack.set_visible_child_name("error")
        return False


class VerboseAddAppDialog(Adw.Window):
    """Enhanced Add Application dialog featuring live repository preview."""

    def __init__(self, parent_window: DebtainiumWindow):
        super().__init__(transient_for=parent_window, modal=True, title="Add Application from GitHub")
        self.parent_win = parent_window
        self.set_default_size(540, 500)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # HeaderBar
        header = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self.destroy())
        header.pack_start(cancel_btn)

        self.add_btn = Gtk.Button(label="Add & Track")
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.connect("clicked", self.on_add_submitted)
        header.pack_end(self.add_btn)

        toolbar_view.add_top_bar(header)

        # Preferences form
        page = Adw.PreferencesPage()
        toolbar_view.set_content(page)

        group_input = Adw.PreferencesGroup(
            title="Repository Specification",
            description="Enter the GitHub repository details to monitor."
        )
        page.add(group_input)

        self.repo_entry = Adw.EntryRow(title="GitHub Repository")
        self.repo_entry.set_text("")
        group_input.add(self.repo_entry)

        # Preview Button
        preview_btn = Gtk.Button(label="Preview & Validate Release Asset")
        preview_btn.add_css_class("pill")
        preview_btn.set_halign(Gtk.Align.END)
        preview_btn.set_margin_top(4)
        preview_btn.set_margin_bottom(4)
        preview_btn.connect("clicked", self.on_preview_clicked)
        group_input.add(preview_btn)

        # Preview Details Box
        self.group_preview = Adw.PreferencesGroup(
            title="Upstream Release Preview",
            description="Live inspection of architecture compatibility."
        )
        self.preview_tag_row = Adw.ActionRow(title="Latest Release")
        self.preview_tag_row.set_subtitle("Not checked yet")
        self.group_preview.add(self.preview_tag_row)

        self.preview_asset_row = Adw.ActionRow(title="Matched .deb Asset")
        self.preview_asset_row.set_subtitle("Enter repository and click Preview")
        self.group_preview.add(self.preview_asset_row)

        page.add(self.group_preview)

        # Advanced Settings
        group_adv = Adw.PreferencesGroup(
            title="Optional Overrides",
            description="Custom display name, package identifier, or asset regex."
        )
        self.name_entry = Adw.EntryRow(title="Display Name (optional)")
        group_adv.add(self.name_entry)

        self.deb_entry = Adw.EntryRow(title="Debian Package Name (optional)")
        group_adv.add(self.deb_entry)

        self.pattern_entry = Adw.EntryRow(title="Asset Regex Filter (optional)")
        group_adv.add(self.pattern_entry)

        self.auto_row = Adw.SwitchRow(title="Install updates automatically")
        self.auto_row.set_subtitle("Include this app in `debtainium update --auto`")
        group_adv.add(self.auto_row)

        page.add(group_adv)

    def on_preview_clicked(self, _btn):
        """Fetch and inspect release asset before adding."""
        repo_raw = self.repo_entry.get_text().strip()
        if not repo_raw:
            self.parent_win.show_toast("Please enter a GitHub repository.")
            return

        try:
            repo = normalize_repo(repo_raw)
        except ValueError as e:
            self.parent_win.show_toast(str(e))
            return

        self.preview_tag_row.set_subtitle("Fetching from GitHub API...")
        self.preview_asset_row.set_subtitle("Matching architecture assets...")

        def worker():
            try:
                release = fetch_latest_release(repo)
                tag = release.get("tag_name", "")
                arch = get_system_arch()
                pattern = self.pattern_entry.get_text().strip()
                asset = pick_release_asset(release, arch=arch, pattern=pattern)

                def update_preview():
                    self.preview_tag_row.set_subtitle(f"Tag: <b>{tag}</b> (published: {release.get('published_at', '')[:10]})")
                    if asset:
                        size_kb = asset.get("size", 0) // 1024
                        self.preview_asset_row.set_subtitle(f"<b>{asset.get('name')}</b> ({size_kb} KB) • Arch: {arch}")
                    else:
                        self.preview_asset_row.set_subtitle(f"<span foreground='#e01b24'>No matching .deb for '{arch}' found!</span>")

                GLib.idle_add(update_preview)
            except Exception as e:
                def on_err():
                    self.preview_tag_row.set_subtitle(f"<span foreground='#e01b24'>Error: {e}</span>")
                    self.preview_asset_row.set_subtitle("Failed to fetch release")

                GLib.idle_add(on_err)

        threading.Thread(target=worker, daemon=True).start()

    def on_add_submitted(self, _btn):
        repo_raw = self.repo_entry.get_text().strip()
        if not repo_raw:
            self.parent_win.show_toast("Repository cannot be empty.")
            return

        try:
            repo = normalize_repo(repo_raw)
        except ValueError as e:
            self.parent_win.show_toast(str(e))
            return

        name = self.name_entry.get_text().strip() or repo.split("/")[-1]
        deb_pkg = self.deb_entry.get_text().strip() or name.lower()
        asset_pattern = self.pattern_entry.get_text().strip()

        for a in self.parent_win.apps:
            if a.repo.lower() == repo.lower():
                self.parent_win.show_toast(f"'{repo}' is already tracked.")
                return

        self.destroy()
        self.parent_win.log(f"Adding new tracked repository: '{repo}' as '{name}' (pkg: '{deb_pkg}')...")

        def fetch_and_add():
            latest = ""
            try:
                release = fetch_latest_release(repo)
                latest = clean_version(release.get("tag_name", ""))
            except Exception as e:
                self.parent_win.log(f"Warning: Could not fetch initial release for {repo}: {e}")

            inst = get_installed_version(deb_pkg) or ""
            new_entry = AppEntry(
                name=name,
                repo=repo,
                deb_pkg=deb_pkg,
                asset_pattern=asset_pattern,
                installed_version=inst,
                latest_version=latest,
                last_checked=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                auto_update=self.auto_row.get_active(),
            )

            def update_ui():
                self.parent_win.apps.append(new_entry)
                save_apps(self.parent_win.apps)
                self.parent_win.log(f"Successfully added '{name}' to tracked applications.")
                self.parent_win.refresh_list_ui()
                self.parent_win.show_toast(f"Added '{name}' to Debtainium.")

            GLib.idle_add(update_ui)

        threading.Thread(target=fetch_and_add, daemon=True).start()


class DebtainiumApplication(Adw.Application):
    """Main Adwaita application class."""

    def __init__(self):
        super().__init__(
            application_id="org.debtainium.Debtainium",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = DebtainiumWindow(self)
        win.present()


def run_gui(args: Optional[List[str]] = None) -> int:
    """Launch the Debtainium GTK4/Libadwaita GUI application."""
    Adw.init()
    app = DebtainiumApplication()
    return app.run(args or sys.argv[:1])
