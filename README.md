# Debtainium

**Debtainium** is an Obtainium-style desktop and CLI utility for Debian and Ubuntu systems. It tracks third-party software distributed as Debian packages (`.deb`) via GitHub Releases, automatically detects architecture-compatible assets, verifies SHA-256 digests, and safely installs and updates software using `pkexec` and `apt`.

![Debtainium Icon](debtainium/usr/share/icons/hicolor/scalable/apps/debtainium.svg)

## Features

- **Standard Library Only Networking:** Uses Python 3.10+ standard library (`urllib.request`) for zero external Python pip dependencies.
- **Modern GTK4 & Libadwaita Interface:** Native desktop UI with toast notifications, asynchronous background threads via `GLib.idle_add`, and progress bars.
- **Full CLI:** Complete command-line support (`add`, `remove`, `list`, `check`, `update`, `notify`).
- **Smart Architecture Detection:** Automatically checks `/releases/latest` and picks `.deb` packages matching `amd64`, `arm64`, `armhf`, or `all` architectures, with optional per-app regex filters.
- **GitHub Rate Limit Mitigation:** Reads `GITHUB_TOKEN` or `GH_TOKEN` environment variables when present.
- **dpkg Version Comparison:** Accurately compares installed versions using `dpkg-query -W -f='${Version}'` against upstream releases via `dpkg --compare-versions`.
- **Integrity Verification:** Verifies SHA-256 digests from companion files or release metadata before installation.
- **Safe Privilege Escalation:** Installs through `pkexec apt-get install -y ./<package>.deb` so dependencies resolve cleanly without ever running the application as root.
- **Background Checks:** Includes a systemd user service and timer that checks for updates every 6 hours and sends desktop notifications via `notify-send`.
- **Self-Updating:** Automatically tracks its own repository so Debtainium can update itself.

---

## Installation

Download the ready-to-install `.deb` package from [Releases](https://github.com/Usman0220/debtainium/releases) and install via APT:

```bash
sudo apt install -y ./debtainium_1.0.0_all.deb
```

Enable the background 6-hour update check timer:

```bash
systemctl --user enable --now debtainium-notify.timer
```

---

## Usage

### Graphical Interface

Launch from your desktop application launcher or run:

```bash
debtainium gui
```

### Command Line Interface

```bash
# List all tracked applications
debtainium list

# Add a new GitHub repository
debtainium add fastfetch-cli/fastfetch

# Check for updates across all tracked applications
debtainium check

# Download, verify, and update an application via polkit
debtainium update fastfetch

# Check for updates and trigger desktop notification
debtainium notify

# Remove a tracked repository
debtainium remove fastfetch
```

---

## Building the .deb Package from Source

```bash
git clone https://github.com/Usman0220/debtainium.git
cd debtainium
chmod +x build-deb.sh
./build-deb.sh
```

The script sets standard Debian file and directory permissions, builds `debtainium_1.0.0_all.deb`, and validates it against `lintian`.

---

## License

MIT License. See [copyright](debtainium/usr/share/doc/debtainium/copyright) for full details.
