"""
Debtainium GTK4 + Libadwaita Graphical Interface
Provides an Obtainium-style desktop interface for tracking, checking,
downloading, and installing .deb packages from GitHub Releases.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from debtainium.core import (
    APP_VERSION,
    AppEntry,
    ChecksumMismatchError,
    DebtainiumError,
    InstallationError,
    clean_version,
    compare_versions,
    download_asset_file,
    extract_sha256_digest,
    fetch_latest_release,
    get_cache_dir,
    get_installed_version,
    get_system_arch,
    inspect_deb,
    install_deb_package,
    is_update_available,
    load_apps,
    normalize_repo,
    pick_release_asset,
    save_apps,
    verify_file_sha256,
)


class DebtainiumWindow(Adw.ApplicationWindow):
    """Main application window for Debtainium."""

    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Debtainium")
        self.set_default_size(780, 580)

        self.apps: List[AppEntry] = []
        self.is_busy = False

        # Toast overlay as root content
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # ToolbarView layout
        self.toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(self.toolbar_view)

        # HeaderBar
        self.header_bar = Adw.HeaderBar()
        title_widget = Adw.WindowTitle(
            title="Debtainium",
            subtitle=f"GitHub .deb Manager (arch: {get_system_arch()})"
        )
        self.header_bar.set_title_widget(title_widget)

        # Header buttons
        self.add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.add_btn.set_tooltip_text("Add Application")
        self.add_btn.connect("clicked", self.on_add_clicked)
        self.header_bar.pack_start(self.add_btn)

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("Check All for Updates")
        self.refresh_btn.connect("clicked", self.on_check_all_clicked)
        self.header_bar.pack_end(self.refresh_btn)

        self.toolbar_view.add_top_bar(self.header_bar)

        # Content Box
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.toolbar_view.set_content(self.content_box)

        # Progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_visible(False)
        self.content_box.append(self.progress_bar)

        # Scrolled container for apps
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.content_box.append(self.scrolled)

        self.clamp = Adw.Clamp(maximum_size=750, tightening_threshold=550)
        self.clamp.set_margin_top(16)
        self.clamp.set_margin_bottom(16)
        self.clamp.set_margin_start(16)
        self.clamp.set_margin_end(16)
        self.scrolled.set_child(self.clamp)

        self.list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.clamp.set_child(self.list_container)

        # Status page when empty
        self.empty_page = Adw.StatusPage(
            icon_name="package-x-generic-symbolic",
            title="No Applications Tracked",
            description="Add GitHub repositories to install and track .deb updates."
        )
        add_empty_btn = Gtk.Button(label="Add Application")
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

        # Initial load
        self.load_data()

    def show_toast(self, message: str, timeout: int = 3):
        """Display an Adw.Toast message."""
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def set_progress_visible(self, visible: bool, fraction: float = 0.0):
        """Toggle progress bar visibility and fraction."""
        self.progress_bar.set_visible(visible)
        self.progress_bar.set_fraction(fraction)

    def load_data(self):
        """Reload apps from disk and refresh UI list."""
        self.apps = load_apps()
        self.refresh_list_ui()

    def refresh_list_ui(self):
        """Render the list of apps into the ListBox."""
        # Clear existing rows
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

        if not self.apps:
            self.empty_page.set_visible(True)
            self.list_box.set_visible(False)
            return

        self.empty_page.set_visible(False)
        self.list_box.set_visible(True)

        for app in self.apps:
            row = self.create_app_row(app)
            self.list_box.append(row)

    def create_app_row(self, app: AppEntry) -> Adw.ActionRow:
        """Create an Adw.ActionRow for an AppEntry."""
        row = Adw.ActionRow()
        row.set_title(app.name)

        inst = get_installed_version(app.deb_pkg) or ""
        app.installed_version = inst
        latest = app.latest_version or "-"

        subtitle = f"Repo: {app.repo}  •  Installed: {inst or 'None'}  •  Latest: {latest}"
        row.set_subtitle(subtitle)

        # Prefix icon
        icon = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
        row.add_prefix(icon)

        # Status badge
        badge = Gtk.Label()
        badge.set_valign(Gtk.Align.CENTER)

        has_update = is_update_available(inst, app.latest_version)
        if not inst:
            badge.set_text("Not Installed")
            badge.add_css_class("dim-label")
            action_btn_label = "Install"
            action_btn_css = "suggested-action"
        elif has_update:
            badge.set_markup("<span foreground='#3584e4'><b>Update Available</b></span>")
            action_btn_label = "Update"
            action_btn_css = "suggested-action"
        else:
            badge.set_markup("<span foreground='#2ec27e'>Up to Date</span>")
            action_btn_label = "Reinstall"
            action_btn_css = "flat"

        row.add_suffix(badge)

        # Action Button (Install / Update / Reinstall)
        action_btn = Gtk.Button(label=action_btn_label)
        action_btn.add_css_class(action_btn_css)
        action_btn.set_valign(Gtk.Align.CENTER)
        action_btn.connect("clicked", lambda b, a=app: self.on_update_app(a))
        row.add_suffix(action_btn)

        # Check button
        check_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.set_tooltip_text("Check for update")
        check_btn.add_css_class("flat")
        check_btn.connect("clicked", lambda b, a=app: self.on_check_single_app(a))
        row.add_suffix(check_btn)

        # Delete button
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.set_tooltip_text("Remove application")
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", lambda b, a=app: self.on_delete_app(a))
        row.add_suffix(del_btn)

        return row

    def on_add_clicked(self, _btn):
        """Open Add Application dialog."""
        dialog = AddAppDialog(self)
        dialog.present()

    def on_delete_app(self, app: AppEntry):
        """Remove an application from tracking."""
        self.apps = [a for a in self.apps if a.repo != app.repo]
        save_apps(self.apps)
        self.refresh_list_ui()
        self.show_toast(f"Removed '{app.name}' from Debtainium.")

    def on_check_single_app(self, app: AppEntry):
        """Check a single app for updates in a background thread."""
        if self.is_busy:
            return
        self.is_busy = True
        self.set_progress_visible(True, 0.3)
        self.show_toast(f"Checking {app.name}...")

        def worker():
            try:
                release = fetch_latest_release(app.repo)
                latest = clean_version(release.get("tag_name", ""))
                app.latest_version = latest
                save_apps(self.apps)
                inst = get_installed_version(app.deb_pkg) or ""
                app.installed_version = inst

                def update_ui():
                    self.set_progress_visible(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    if is_update_available(inst, latest):
                        self.show_toast(f"Update available for {app.name}: {latest}")
                    else:
                        self.show_toast(f"{app.name} is up to date ({inst or latest})")

                GLib.idle_add(update_ui)
            except Exception as e:
                def on_err():
                    self.set_progress_visible(False)
                    self.is_busy = False
                    self.show_toast(f"Error checking {app.name}: {e}", timeout=5)

                GLib.idle_add(on_err)

        threading.Thread(target=worker, daemon=True).start()

    def on_check_all_clicked(self, _btn):
        """Check all apps for updates in a background thread."""
        if self.is_busy or not self.apps:
            return
        self.is_busy = True
        self.set_progress_visible(True, 0.1)
        self.show_toast("Checking all apps for updates...")

        def worker():
            total = len(self.apps)
            updates_count = 0
            for idx, app in enumerate(self.apps):
                try:
                    frac = (idx + 1) / total
                    GLib.idle_add(lambda f=frac: self.set_progress_visible(True, f))
                    release = fetch_latest_release(app.repo)
                    latest = clean_version(release.get("tag_name", ""))
                    app.latest_version = latest
                    inst = get_installed_version(app.deb_pkg) or ""
                    app.installed_version = inst
                    if is_update_available(inst, latest):
                        updates_count += 1
                except Exception as e:
                    sys.stderr.write(f"Check failed for {app.name}: {e}\n")

            save_apps(self.apps)

            def done_ui():
                self.set_progress_visible(False)
                self.refresh_list_ui()
                self.is_busy = False
                if updates_count > 0:
                    self.show_toast(f"Check complete: {updates_count} update(s) available!")
                else:
                    self.show_toast("All applications are up to date.")

            GLib.idle_add(done_ui)

        threading.Thread(target=worker, daemon=True).start()

    def on_update_app(self, app: AppEntry):
        """Download, verify, and install app in a background thread."""
        if self.is_busy:
            return
        self.is_busy = True
        self.set_progress_visible(True, 0.05)
        self.show_toast(f"Fetching release for {app.name}...")

        def worker():
            try:
                # 1. Fetch release
                release = fetch_latest_release(app.repo)
                latest = clean_version(release.get("tag_name", ""))
                app.latest_version = latest

                # 2. Pick asset
                arch = get_system_arch()
                asset = pick_release_asset(release, arch=arch, pattern=app.asset_pattern)
                if not asset:
                    raise DebtainiumError(f"No .deb asset matching arch '{arch}' found in release {latest}")

                asset_name = asset["name"]
                download_url = asset["browser_download_url"]

                # 3. Download
                repo_slug = app.repo.replace("/", "_")
                dest_file = get_cache_dir() / repo_slug / latest / asset_name

                GLib.idle_add(lambda: self.show_toast(f"Downloading {asset_name}..."))

                def progress(downloaded: int, total: int):
                    if total > 0:
                        fraction = max(0.1, min(0.85, downloaded / total))
                        GLib.idle_add(lambda f=fraction: self.set_progress_visible(True, f))

                if not dest_file.exists():
                    download_asset_file(download_url, dest_file, progress)

                # 4. SHA-256 verification
                expected_hash = extract_sha256_digest(release, asset_name)
                if expected_hash:
                    GLib.idle_add(lambda: self.show_toast("Verifying SHA-256 digest..."))
                    if not verify_file_sha256(dest_file, expected_hash):
                        dest_file.unlink(missing_ok=True)
                        raise ChecksumMismatchError("SHA-256 digest mismatch! File was deleted.")

                # 5. Inspect deb to ensure correct deb_pkg
                info = inspect_deb(dest_file)
                if info.get("Package"):
                    app.deb_pkg = info["Package"]

                # 6. Install via pkexec apt-get
                GLib.idle_add(lambda: self.show_toast("Installing package via pkexec (check prompt)..."))
                GLib.idle_add(lambda: self.set_progress_visible(True, 0.95))

                install_deb_package(dest_file)

                # 7. Update status
                new_inst = get_installed_version(app.deb_pkg) or latest
                app.installed_version = new_inst
                save_apps(self.apps)

                def success_ui():
                    self.set_progress_visible(False)
                    self.refresh_list_ui()
                    self.is_busy = False
                    self.show_toast(f"Successfully installed {app.name} ({new_inst})!", timeout=4)

                GLib.idle_add(success_ui)

            except Exception as e:
                def err_ui():
                    self.set_progress_visible(False)
                    self.is_busy = False
                    self.show_toast(f"Failed to install {app.name}: {e}", timeout=6)

                GLib.idle_add(err_ui)

        threading.Thread(target=worker, daemon=True).start()


class AddAppDialog(Adw.Window):
    """Dialog window to add a new tracked GitHub repository."""

    def __init__(self, parent_window: DebtainiumWindow):
        super().__init__(transient_for=parent_window, modal=True, title="Add Application")
        self.parent_win = parent_window
        self.set_default_size(480, 360)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # HeaderBar
        header = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self.destroy())
        header.pack_start(cancel_btn)

        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self.on_add_submitted)
        header.pack_end(add_btn)

        toolbar_view.add_top_bar(header)

        # Preferences form
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Repository Information",
            description="Enter the GitHub repository details to track."
        )
        page.add(group)

        self.repo_entry = Adw.EntryRow(title="GitHub Repository")
        self.repo_entry.set_text("")
        group.add(self.repo_entry)

        self.name_entry = Adw.EntryRow(title="Display Name (optional)")
        group.add(self.name_entry)

        self.deb_entry = Adw.EntryRow(title="Debian Package Name (optional)")
        group.add(self.deb_entry)

        self.pattern_entry = Adw.EntryRow(title="Asset Regex Filter (optional)")
        group.add(self.pattern_entry)

        toolbar_view.set_content(page)

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

        # Check existing
        for a in self.parent_win.apps:
            if a.repo.lower() == repo.lower():
                self.parent_win.show_toast(f"'{repo}' is already tracked.")
                return

        self.destroy()
        self.parent_win.show_toast(f"Adding '{name}'...")

        def fetch_and_add():
            latest = ""
            try:
                release = fetch_latest_release(repo)
                latest = clean_version(release.get("tag_name", ""))
            except Exception as e:
                sys.stderr.write(f"Initial release check failed: {e}\n")

            inst = get_installed_version(deb_pkg) or ""
            new_entry = AppEntry(
                name=name,
                repo=repo,
                deb_pkg=deb_pkg,
                asset_pattern=asset_pattern,
                installed_version=inst,
                latest_version=latest,
            )

            def update_ui():
                self.parent_win.apps.append(new_entry)
                save_apps(self.parent_win.apps)
                self.parent_win.refresh_list_ui()
                self.parent_win.show_toast(f"Added '{name}' to Debtainium.")

            GLib.idle_add(update_ui)

        threading.Thread(target=fetch_and_add, daemon=True).start()


class DebtainiumApplication(Adw.Application):
    """Main Adwaita application class."""

    def __init__(self):
        super().__init__(
            application_id="org.debtainium.Debtainium",
            flags=gi.repository.Gio.ApplicationFlags.FLAGS_NONE,
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
