# Docker Run Notes

Docker is useful for repeatable testing, but Reolink UID/P2P uses UDP. On Docker Desktop for Windows, prefer host networking when available.

## Option A: Docker Desktop Host Networking

Requires Docker Desktop 4.34 or newer.

1. Open Docker Desktop.
2. Go to Settings -> Resources -> Network.
3. Enable host networking.
4. Apply and restart Docker Desktop.

Then run from this folder:

```powershell
$env:DOCKER_CONFIG="$PWD\.dockerconfig"
New-Item -ItemType Directory -Force .dockerconfig | Out-Null
docker build -t pyneolink .
docker run --rm --network host `
  -v "${PWD}\config.json:/app/config.json:ro" `
  -v "${PWD}\.pyneolink_state.json:/app/.pyneolink_state.json" `
  pyneolink --info --config config.json --camera "Scherbaka 41 - Front"
```

## Option B: UDP Port Mapping

Use this if host networking is unavailable:

```powershell
$env:DOCKER_CONFIG="$PWD\.dockerconfig"
New-Item -ItemType Directory -Force .dockerconfig | Out-Null
docker build -t pyneolink .
docker run --rm `
  -p 16577:16577/udp `
  -p 8554:8554/tcp `
  -v "${PWD}\config.json:/app/config.json:ro" `
  -v "${PWD}\.pyneolink_state.json:/app/.pyneolink_state.json" `
  pyneolink --info --config config.json --camera "Scherbaka 41 - Front"
```

Add `--debug` at the end only when diagnosing a failed connection. Debug output is verbose and may include camera internals.

## If Docker Says Permission Denied

Start Docker Desktop and make sure your Windows user can access Docker. Usually this means adding the user to the `docker-users` group, then signing out and back in.
