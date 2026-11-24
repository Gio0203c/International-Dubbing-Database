# Scheduled Sync Setup

The Wikidata sync pipeline is designed to run once a week as a full refresh. Two standard options are supported on Linux hosts.

## Option 1: systemd timer

Copy the unit files under `ops/systemd/` into `/etc/systemd/system/` (or `~/.config/systemd/user/` for a user timer), replace `/path/to/International-Dubbing-Database` with your local checkout path, and enable the timer:

```bash
sudo cp ops/systemd/iddb-wikidata-sync.service /etc/systemd/system/
sudo cp ops/systemd/iddb-wikidata-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iddb-wikidata-sync.timer
```

Check timer status and logs with:

```bash
systemctl list-timers iddb-wikidata-sync.timer
journalctl -u iddb-wikidata-sync.service -n 50
```

## Option 2: cron

For environments without systemd (such as WSL without systemd enabled), add a weekly entry with `crontab -e`:

```cron
0 3 * * 0 cd /path/to/International-Dubbing-Database && .venv/bin/python -m etl.run_wikidata_sync >> /var/log/iddb-wikidata-sync.log 2>&1
```
