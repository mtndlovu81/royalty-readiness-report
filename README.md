# Royalty Readiness Report (r³)

A public, no-login tool that shows an artist which of their songs are missing
the identifiers that royalty payments depend on.

Search a name, open a profile, and every song is checked against the three
codes the money actually moves on. Nothing to install, nothing to sign up for.

[Demo](https://www.loom.com/share/8d719dae768c431f97cde3ae31cfe06d) - https://www.loom.com/share/8d719dae768c431f97cde3ae31cfe06d

---

## The problem

Three identifiers decide whether money reaches the person who made the music:

| Identifier | Attached to | If it's missing |
|---|---|---|
| **ISRC** | a specific recording | streaming plays may not match to the artist |
| **ISWC** | the underlying composition | mechanical and performance royalties have nothing to attach to |
| **IPI** | a songwriter | collection societies can't route that writer's share |

Royalties that can't be matched sit in what the industry calls the black box,
and after roughly two to three years they're reallocated — usually by market
share, to the top earners. The money doesn't wait for anyone to notice, and
music creators or rightsholders can lose earnings without ever knowing it
happened.

This tool lets independent artists and songwriters check whether their work is
ready to collect, so they can fix problems before their royalties end up in the
black box.

### What it is not

Not a registration service, not a metadata editor, not a rights database. It
reports; the artist acts.

---

## The honesty constraint

The data comes from [MusicBrainz](https://musicbrainz.org), which is
volunteer-edited. **An empty field there means "nobody entered it" far more
often than it means "this doesn't exist."** An artist can be correctly
registered, hold a valid IPI, and have distributor-issued ISRCs, and MusicBrainz
may show none of it.

So the tool never asserts a fact about anyone's registration. Every message says
what was found, why that might be, what it would cost if the gap is real, and
what to do:

> **No publishing ID found**
> We couldn't find one in our sources — that may mean the composition isn't
> registered, or just that our sources don't have it yet. If it isn't
> registered, mechanical and performance royalties have nothing to attach to.
> Check with your PRO.

Severity carries the consequence; the wording carries the uncertainty. A red
flag stays red — it just never accuses.

The same rule governs the reassuring states. A green tick means *found in our
sources*, never *correct* — a present ISWC proves a code exists, not that the
writer splits behind it are right.

---

## Running it locally

Requires Python 3.12+ and PostgreSQL 12+.

```bash
git clone <repository-url>
cd royalty-readiness-report

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

createdb r3                       # or: sudo -u postgres createuser r3 --pwprompt
cp .env.example .env              # then edit it — see below
python scripts/init_db.py         # creates 10 tables
```

The schema uses the `pgcrypto` and `unaccent` extensions. `init_db.py` creates
both; neither needs superuser. `pgcrypto` is required on PostgreSQL 12 because
`gen_random_uuid()` only became built-in in 13.

Two processes. In one terminal:

```bash
uvicorn r3.main:app --reload      # http://localhost:8000
```

In another, the build worker — **profiles only get built while this runs**:

```bash
RUN_WORKER=true python scripts/worker.py
```

Then search for an artist. If they aren't in the catalogue yet, clicking the
result queues a build and the status page follows its progress.

To pre-build a set of well-known artists:

```bash
python scripts/seed.py            # queue them
python scripts/seed.py --check    # see the spread of results
```

### Configuration

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `MB_USER_AGENT` | **Required.** Must include real contact details — see below |
| `MB_RATE_LIMIT_SECONDS` | Defaults to `1.0` and is clamped there; it cannot be lowered |
| `TRUST_PROXY_HEADERS` | `true` only behind the load balancer — see below |
| `RUN_WORKER` | `true` on one machine only |
| `STALE_AFTER_DAYS` | How long a cached contributor stays fresh |

`.env` is gitignored. `.env.example` documents every variable.

---

## The external API, and why there's no key

Data comes from the [MusicBrainz Web Service
v2](https://musicbrainz.org/doc/MusicBrainz_API). It's a public API and
**requires no API key**.

MusicBrainz asks for two things instead, and both are enforced in code:

**1. A descriptive User-Agent with working contact details.** Requests without
one are blocked. It's set from `MB_USER_AGENT`, and the app refuses to start if
that's still a placeholder — a misconfiguration that would otherwise show up as
a wall of blocked requests much later.

**2. No more than one request per second.** Enforced by a process-wide gate that
every request passes through, retries included. `MB_RATE_LIMIT_SECONDS` is
clamped to a 1.0s floor, so a typo in `.env` can't breach it.

Data is licensed under [CC0 and CC BY-NC-SA](https://musicbrainz.org/doc/About/Data_License).
Core identifiers are public domain; the tool credits MusicBrainz on every page.

The single hardest constraint in the whole project is that one request per
second. A large catalogue is 100+ requests, which is minutes of wall clock — and
that shapes the architecture below.

---

## Architecture

```
                      ┌──────────────┐
    visitors ───────► │     Lb01     │  HAProxy round-robin + PostgreSQL
                      └──────┬───────┘
                     ┌───────┴────────┐
              ┌──────▼─────┐   ┌──────▼─────┐
              │   Web01    │   │   Web02    │   uvicorn, stateless
              │ + worker   │   └────────────┘
              └──────┬─────┘
                     │  ← the only process that talks to MusicBrainz
                     ▼
               MusicBrainz API
```

**Reads never wait on the network.** A profile is assembled from Postgres in
four queries regardless of catalogue size. Nothing in a request path fetches a
catalogue.

**One worker, on Web01 only.** The rate limiter is a token bucket held in a
single process. Two workers would be two buckets, twice the outbound rate, and a
block that takes the whole application down. `RUN_WORKER` guards the entrypoint
as a second line of defence behind the systemd unit.

**Stateless web servers.** No login means no sessions, which means no sticky
sessions and no shared session store — either server can answer any request.

The one exception to "reads don't call upstream": searching a name we hold
nothing for makes a single gated request to ask *who do you mean?*, and the
answer is cached in Postgres for 24 hours. No catalogue is walked; nothing is
built until someone asks for it.

### Build pipeline

Building a catalogue browses releases, collapses them to release-groups, picks
one canonical release per group, then fetches that release's recordings — rather
than browsing every recording an artist has.

The difference is large. Browsing recordings for Radiohead returns 12,470 rows
against 214 compositions: a 58x inflation, almost all bootlegs and compilation
appearances. Release-first returns the catalogue an artist would recognise as
theirs, in roughly half the requests.

---

## Deployment

Three Ubuntu servers. `deploy/` holds the systemd units and the HAProxy config.

**On both web servers:**

```bash
sudo cp deploy/r3-web.service /etc/systemd/system/
sudo systemctl enable --now r3-web
```

**On Web01 only** — installing this on Web02 doubles the outbound rate and gets
the application blocked:

```bash
sudo cp deploy/r3-worker.service deploy/r3-worker.timer /etc/systemd/system/
sudo systemctl enable --now r3-worker.timer
```

**On Lb01:**

```bash
sudo cp /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.bak
sudo cp deploy/haproxy.cfg /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg    # validate before restarting
sudo systemctl restart haproxy
```

`deploy/setup-web.sh` automates the web server steps end to end — clone or pull,
virtualenv, dependencies, `.env`, systemd units, restart, health check. It is
idempotent and doubles as the redeploy script:

```bash
sudo ./deploy/setup-web.sh                      # Web02
RUN_WORKER=true sudo -E ./deploy/setup-web.sh   # Web01
```

It never overwrites an existing `.env`, and on the non-worker server it actively
removes the worker units rather than merely not installing them.

Postgres also runs on Lb01, bound to the private interface only, with
`pg_hba.conf` restricted to the two web servers' private addresses. Port 5432 is
never exposed publicly.

Set `TRUST_PROXY_HEADERS=true` on both web servers. Behind HAProxy every request
arrives from Lb01's address, so without it the per-IP rate limiter treats the
entire internet as one visitor and throttles the site collectively. Leave it
`false` anywhere reachable directly, where a forged header would let anyone
bypass limiting entirely.

The HAProxy config sets `X-Real-IP` with `http-request set-header` rather than
`add-header`. `set-header` replaces, so a client sending a forged value has it
overwritten; appending would let anyone bypass per-IP limiting by spoofing the
header.

### Verifying the load balancing

Check both backends are in rotation:

```bash
echo "show stat" | sudo socat stdio /run/haproxy/admin.sock | cut -d, -f1,2,18
```

Both `web01` and `web02` should report `UP`. To watch requests alternate:

```bash
sudo tail -f /var/log/haproxy.log | grep -o 'r3_back/web0[12]'
```

Then stop one server and confirm the site stays up:

```bash
sudo systemctl stop r3-web             # on Web01
curl -s http://<lb-address>/health     # still ok, served by Web02
sudo systemctl start r3-web
```

HAProxy checks `/health` every 5 seconds, marks a backend down after 3
consecutive failures and returns it after 2 successes. `/health` touches neither
the database nor the upstream API, so a slow query can't pull a healthy server
out of rotation.

`deploy/verify.sh` runs all of the above as one command, including the failover
test and a check that the worker timer is enabled on exactly one server.

---

## Challenges

### Application

**A probe that looked like a failure and wasn't.** Early testing suggested the
API was ignoring the parameters that return ISRCs and composition links —
25 recordings came back with neither. The includes were working the whole time:
the browse endpoint orders by internal ID, so the first page of a large
catalogue is obscure live takes and bootlegs that genuinely have no data. The
lesson went into the client: a 400 is definitive, a 200 with empty relations is
not.

**Flagging every band as critically broken.** The design originally flagged a
missing artist IPI as red. Testing showed MusicBrainz returns an empty IPI list
for Radiohead and three for Björk — both correct, because IPIs identify people,
not groups. Shipped as written, every band using the tool would have been marked
critically broken for something that isn't broken and can't be fixed. The flag
is now gated on artist type, and the same rule had to be applied to
contributors, since bands are routinely credited as writers on their own songs.

**A profile that was 95% red.** With real data, Portishead read "72 of 76 songs
need attention" — including a twelve-second applause clip from a live album,
flagged for having no publishing ID. Technically defensible, and useless: an
artist who sees that concludes the tool doesn't understand their catalogue, and
the honest red flags beside it lose their weight. Songs are now classified as
primary or secondary catalogue from the release types they appear on, and only
the primary catalogue feeds the headline. The same artist now reads 34 of 38,
with the live and compilation material still listed below, still checked, just
not counted.

**A CSV export that could run code.** Song titles come from a volunteer-edited
source, so a track called `=cmd|'/c calc'!A1` is entirely possible — and Excel,
Sheets and LibreOffice all execute that on open. A spreadsheet downloaded from
this tool would have been a delivery mechanism. Cells beginning with a formula
character are now escaped.

**A database outage that hung instead of erroring.** With Postgres unreachable,
pages didn't fail — they hung for 30 seconds and then returned a generic error.
The connection pool waits its default timeout and raises a pool-specific
exception that the database error handler wasn't catching. Now it fails in five
seconds with an honest message, and returns 503 rather than 500, so a shared
database fault doesn't cause the load balancer to drain healthy web servers one
by one.

### Deployment

**Servers that couldn't run the code.** The provided machines run Ubuntu 20.04
with Python 3.8; the codebase needs 3.10+ for its type annotations. The usual
backport route — the deadsnakes PPA — added cleanly and then resolved nothing:
the servers are arm64 and deadsnakes publishes amd64 only, so the repository was
simply empty for this architecture. Built CPython 3.12 from source with `make
altinstall`, which installs alongside `/usr/bin/python3` rather than replacing
the interpreter that apt, ufw and cloud-init depend on. Upgrading the OS would
have meant two release upgrades on machines with no console access, and the only
thing needed from a newer OS was the interpreter.

**A load balancer that couldn't reach its backends.** The web service unit bound
uvicorn to `127.0.0.1`, written when the design assumed a reverse proxy on each
web server. HAProxy runs on a separate machine and connects over the private
network, so loopback made both backends unreachable: health checks fail, every
request returns 503, and the application looks perfectly healthy when curled
locally. The deploy script now verifies health on the private interface as well
as localhost, because the localhost check alone would have passed.

**Firewalls between instances.** Connections from the web servers to Postgres
timed out rather than being refused — packets going nowhere rather than a
service declining them, which pointed at a firewall rather than configuration.
`ufw` on the load balancer was blocking inter-instance traffic; the same rule
was then needed on both web servers for port 8000 before HAProxy's health checks
could succeed.

**A queue that stopped draining silently.** A worker claimed a build and was
killed mid-run. Because the worker selects only rows marked `queued`, the
claimed row became permanently invisible — the queue never drained while every
subsequent run reported success and built nothing. The fix is a stale-claim
timeout: rows stuck in `running` past a threshold are reclaimed, with an attempt
cap so a genuinely broken artist is marked failed rather than retried forever.

---

## Assignment requirements

| Requirement | Where |
|---|---|
| External API | MusicBrainz WS/2, credited above with licensing |
| Meaningful purpose | unmatched royalties are reallocated after 2–3 years |
| Search / filter / sort | artist search; in-page filter; sort by severity, title or date |
| Clear presentation | headline figure, per-song table, expandable per-category report |
| Error handling | every failure mode returns a styled page; no stack trace reaches a visitor |
| API keys secure | none required; `.env` gitignored; the User-Agent obligation documented |

### Bonus work claimed

**Caching.** The entire persistence layer: profile reads never touch the
network. Search results are cached for 24 hours, contributors are cached
globally — a producer credited across forty artists is fetched once, ever — and
an upstream outage serves stale cache rather than an error.

**Security.** Parameterized queries throughout; Jinja2 autoescaping never
bypassed; sort parameters whitelisted against a fixed set; Spotify URLs parsed
and validated; per-IP rate limiting on every route that can reach upstream;
honeypot fields on both forms; CSV formula injection blocked; the report form's
return path validated against open redirect; Postgres bound to a private
interface with host-based access limited to two addresses.

**Performance.** Release-first fetching (roughly half the requests of the
obvious approach, for better data); a global contributor cache that makes each
successive build cheaper; a seeding script so recognisable artists resolve
instantly.

**Considered and deferred:** Docker, Kubernetes and CI/CD were all in scope for
the bonus and were left out at three days. The deployment is systemd units, an
HAProxy config and two shell scripts, which is honest about what it is.

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers the flag rules, the throttled API client's retry and backoff behaviour,
and slug generation — the three places where a bug is invisible rather than
obvious. Routes and templates are verified by looking at them.

---

## Author

[Mthabisi Ndlovu](https://github.com/mtndlovu81)

## Credits

Artist, recording and composition data from
[MusicBrainz](https://musicbrainz.org), used under its
[data licences](https://musicbrainz.org/doc/About/Data_License). MusicBrainz is
a community-maintained open music encyclopedia; if this tool is useful to you,
[contributing corrections back](https://musicbrainz.org/doc/How_to_Contribute)
helps everyone who uses it — including the next artist who looks themselves up.
