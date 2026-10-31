# Debtainium

**Debtainium** is an Obtainium-style desktop and CLI utility for Debian and Ubuntu systems. It tracks third-party software distributed as Debian packages (`.deb`) via GitHub Releases, automatically detects architecture-compatible assets, verifies SHA-256 digests, and safely installs and updates software using `pkexec` and `apt`.

![Debtainium Icon](debtainium/usr/share/icons/hicolor/scalable/apps/debtainium.svg)

## Features

- **Standard Library Only Networking:** Uses Python 3.10+ standard library (`urllib.request`) for zero external Python pip dependencies.
- **Modern GTK4 & Libadwaita Interface:** Native desktop UI with toast notifications, asynchronous background threads via `GLib.idle_add`, progress bars, an in-app release notes viewer, and a status-aware search filter.
- **Full CLI:** `add`, `remove`, `list`, `check`, `update`, `auto`, `export`, `import`, `notify`, `gui` — plus `--json` output for scripting.
- **Smart Architecture Detection:** Picks `.deb` packages matching `amd64`, `arm64`, `armhf`, or `all` architectures, with optional per-app regex filters.
- **Update All:** One button (or `debtainium update --all`) checks every tracked app, then installs each available update in turn and reports per-app failures.
- **Automatic Installation Policy:** Flag individual apps with `debtainium auto <app> on` or the switch in the GUI; `debtainium update --auto` installs only those. A systemd user timer can run that policy daily.
- **Import / Export:** Move the tracked list between machines as portable JSON (`debtainium export`, `debtainium import`). Imports merge by repository, so repeating one is harmless.
- **GitHub Rate Limit Mitigation:** Reads `GITHUB_TOKEN` or `GH_TOKEN`, or a token file at `~/.config/debtainium/token`.
- **dpkg Version Comparison:** Accurately compares installed versions using `dpkg-query -W -f='${Version}'` against upstream releases via `dpkg --compare-versions`.
- **Integrity Verification:** Verifies SHA-256 digests from companion files or release metadata before installation.
- **Safe Privilege Escalation:** Installs through `pkexec apt-get install -y <absolute path>.deb` so dependencies resolve cleanly without ever running the application as root.
- **Background Checks:** systemd user service and timer check for updates every 6 hours and send desktop notifications via `notify-send`.
- **Config Safety:** Saves are atomic, the previous file is kept as `apps.json.bak`, and an unparseable config is preserved as `apps.json.corrupt-<timestamp>` instead of being replaced by an empty list.
- **Self-Updating:** Automatically tracks its own repository so Debtainium can update itself.

---

## Installation

Download the ready-to-install `.deb` package from [Releases](https://github.com/Usman0220/debtainium/releases) and install via APT:

```bash
sudo apt install -y ./debtainium_1.1.0_all.deb
```

Enable the background 6-hour update check timer:

```bash
systemctl --user enable --now debtainium-notify.timer
```

Optional — install flagged updates automatically once a day:

```bash
# Unattended installs need a polkit rule; see the file for the trade-off.
sudo groupadd --system debtainium
sudo usermod -aG debtainium "$USER"     # then log out and back in
sudo install -m 0644 \
  /usr/share/doc/debtainium/examples/49-debtainium-autoupdate.rules \
  /etc/polkit-1/rules.d/49-debtainium-autoupdate.rules
systemctl --user enable --now debtainium-autoupdate.timer
```

> **Read the rule before installing it.** It lets members of the `debtainium` group run `apt-get` through `pkexec` without a password, which is effectively passwordless root for package management. The file documents this in full. Without the rule, everything still works — new releases are only *notified*, and installations ask for your password.

---

## Usage

### Graphical Interface

Launch from your desktop application launcher or run:

```bash
debtainium gui
```

The header has **Update All**, a check-all refresh button, a search filter that understands `update`, `not installed`, `up to date`, and `auto`, and a main menu for import/export, enabling or disabling the background timer, and the About dialog. Each application row expands to show its repository, package identifier, version status, asset filter, an auto-update switch, and an in-app release notes viewer.

### Command Line Interface

```bash
# List all tracked applications (add --json for machine-readable output)
debtainium list

# Add a new GitHub repository, optionally opting into automatic installs
debtainium add fastfetch-cli/fastfetch
debtainium add muesli/duf --auto

# Turn the automatic policy on or off for one app
debtainium auto duf off

# Check for updates across all tracked applications
debtainium check

# Download, verify, and install a single app via polkit
debtainium update fastfetch

# Check everything and install every available update
debtainium update --all

# Install only the apps flagged for automatic updates
debtainium update --auto

# Back up and restore the tracked list
debtainium export -f ~/debtainium-apps.json
debtainium import ~/debtainium-apps.json          # merge
debtainium import ~/debtainium-apps.json --replace

# Check for updates and trigger desktop notification
debtainium notify

# Remove a tracked repository
debtainium remove fastfetch
```

### Private repositories and rate limits

Anonymous GitHub API requests are limited to 60 per hour. Supply a token either through the environment or a file:

```bash
# environment
export GITHUB_TOKEN=ghp_...

# or a file, which is what the systemd timer will see
install -m 600 /dev/null ~/.config/debtainium/token
printf '%s\n' 'ghp_...' > ~/.config/debtainium/token
```

Debtainium warns on stderr if the token file is readable by other users.

### Configuration locations

| Path | Purpose |
|---|---|
| `~/.config/debtainium/apps.json` | tracked applications |
| `~/.config/debtainium/apps.json.bak` | previous version of the file |
| `~/.config/debtainium/token` | optional GitHub token |
| `~/.cache/debtainium/` | downloaded `.deb` packages |

Both honour `XDG_CONFIG_HOME` and `XDG_CACHE_HOME`.

---

## Building the .deb Package from Source

```bash
git clone https://github.com/Usman0220/debtainium.git
cd debtainium
chmod +x build-deb.sh
./build-deb.sh
```

The script sets standard Debian file and directory permissions, validates the AppStream metadata and desktop entry when the tools are available, builds `debtainium_1.1.0_all.deb`, and runs `lintian`.

---

## License

MIT License. See [copyright](debtainium/usr/share/doc/debtainium/copyright) for full details.
