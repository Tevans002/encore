# Running Neo4j for the Chapter 7 Examples

`ch07_04_neo4j_simple.py` (and your `query_artist_graph` tool) need a Neo4j
server listening on **bolt://localhost:7687** with user `neo4j` and the
password in `NEO4J_PASSWORD` (the scripts default to `password`).

Pick **one** of the options below. Docker is the fastest; a local install is
fine if you can't run Docker.

| Port | What it's for |
| ---- | ------------- |
| 7474 | Neo4j Browser, a web UI at http://localhost:7474 for typing Cypher by hand |
| 7687 | Bolt, the protocol the Python driver uses |

> Passwords must be at least 8 characters. `password` works for these demos;
> never reuse a real password.

---

## Option A — Docker (recommended)

Install Docker Desktop (macOS/Windows) or Docker Engine (Linux) first.

### A1. One command

macOS / Linux:

```bash
mkdir -p "$HOME/neo4j/data"

docker run -d --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  -v "$HOME/neo4j/data:/data" \
  neo4j:2026.07.1
```

Windows (PowerShell):

```powershell
mkdir $HOME\neo4j\data -Force

docker run -d --name neo4j `
  -p 7474:7474 -p 7687:7687 `
  -e NEO4J_AUTH=neo4j/password `
  -v "$HOME\neo4j\data:/data" `
  neo4j:2026.07.1
```

- `-d` runs it in the background; `--name neo4j` lets you refer to it later.
- `-v` keeps your data on your machine, so it survives restarts. **Create the
  folder first**, or Neo4j fails to start with a permissions error.
- The tag is pinned to a specific version. Any newer `neo4j:2026.x` tag also
  works. With no suffix you get the free Community Edition.
- `NEO4J_AUTH` only applies the first time. If the data folder already holds
  a database, the old password stays. To start over, delete the folder.

### A2. Or with Docker Compose

Use the `docker-compose.yml` in this folder:

```bash
mkdir neo4j_data
docker compose up -d
```

### Day-to-day commands

```bash
docker logs -f neo4j     # watch startup; wait for "Started."  (Ctrl+C to stop watching)
docker stop neo4j        # stop it
docker start neo4j       # start it again (data is still there)
docker rm -f neo4j       # delete the container (data folder is kept)
```

With Compose: `docker compose logs -f`, `docker compose stop`,
`docker compose start`, `docker compose down`.

---

## Option B — Install locally

Neo4j 2026.x needs **Java 21** (Java 25 also works). Check with:

```bash
java -version
```

If it's missing, install a JDK 21 such as Eclipse Temurin
(https://adoptium.net) or Azul Zulu.

> **Easiest local option on macOS/Windows:** Neo4j Desktop
> (https://neo4j.com/download/) is a GUI app that bundles its own Java.
> Create a local database there, set the password, click **Start**,
> and skip the rest of this section.

### B1. Ubuntu / Debian (apt)

```bash
# Add Neo4j's package repository
sudo mkdir -p /etc/apt/keyrings
wget -O - https://debian.neo4j.com/neotechnology.gpg.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/neotechnology.gpg
sudo chmod a+r /etc/apt/keyrings/neotechnology.gpg
echo 'deb [signed-by=/etc/apt/keyrings/neotechnology.gpg] https://debian.neo4j.com stable latest' \
  | sudo tee /etc/apt/sources.list.d/neo4j.list
sudo apt-get update

# Install Community Edition
sudo apt-get install neo4j

# Set the password BEFORE the first start
sudo neo4j-admin dbms set-initial-password password

# Start now and on every boot
sudo systemctl enable --now neo4j
systemctl status neo4j
```

### B2. macOS

Homebrew:

```bash
brew install neo4j
neo4j-admin dbms set-initial-password password
neo4j start            # or: brew services start neo4j
```

Or the tarball: download the **Linux / Mac Executable (tar)** from the Neo4j
Deployment Center (https://neo4j.com/deployment-center/), then:

```bash
tar -xf neo4j-community-*-unix.tar.gz
export NEO4J_HOME="$PWD/neo4j-community-<VERSION>"

"$NEO4J_HOME/bin/neo4j-admin" dbms set-initial-password password
"$NEO4J_HOME/bin/neo4j" console      # foreground; Ctrl+C to stop
# or: "$NEO4J_HOME/bin/neo4j" start  # background; "neo4j stop" to stop
```

### B3. Windows (zip)

Download the **Windows Executable (zip)** from the Neo4j Deployment Center,
right-click → **Extract All** (for example to `C:\neo4j`), then in a terminal:

```bat
setx NEO4J_HOME "C:\neo4j\neo4j-community-<VERSION>"
REM open a NEW terminal so NEO4J_HOME is set

%NEO4J_HOME%\bin\neo4j-admin dbms set-initial-password password
%NEO4J_HOME%\bin\neo4j console
```

To run it as a background Windows service instead (admin terminal):

```bat
%NEO4J_HOME%\bin\neo4j windows-service install
%NEO4J_HOME%\bin\neo4j start
```

---

## Check that it works

1. Open http://localhost:7474, log in as `neo4j` / `password`, and run:

   ```cypher
   RETURN "hello" AS ok
   ```

2. Run the example:

   ```bash
   pip install neo4j
   python ch07_04_neo4j_simple.py
   ```

   The first line should say `Connected to bolt://localhost:7687`.

3. To see the demo graph, run this in the Browser:

   ```cypher
   MATCH (n:Ch07Demo) RETURN n
   ```

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `ServiceUnavailable` / connection refused | Neo4j isn't running or is still starting. Wait for `Started.` in the logs. |
| `AuthError` / unauthorized | Wrong password. Set `NEO4J_PASSWORD`, or (Docker) delete the data folder and recreate the container. |
| Docker: "port is already allocated" | Another Neo4j is running. Stop it, or map different ports, e.g. `-p 7688:7687` and `NEO4J_URI=bolt://localhost:7688`. |
| Local: Java error on start | `java -version` must show 21 (or 25). |
| Campus Wi-Fi blocks Docker Hub | Pull the image at home first: `docker pull neo4j:2026.07.1`. |

**Security:** these settings are for a laptop only. Don't expose ports
7474/7687 to the internet with a demo password, and don't commit `.env`
files or the `neo4j/data` folder.
