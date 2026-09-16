# Operator ergonomics for the factory (§12.4).
#
# These recipes are convenience only — every workflow script stays directly runnable, so
# `uv run --script factory/f_prompt.py "..."` must always work without `just`.
#
# There is no `sqlite3` CLI on this machine, so every trace query reads the database through
# Python rather than shelling out to a binary that is not there.

_default:
    @just --list

# --- workflow recipes -------------------------------------------------------------------
# One recipe per FW. Convenience only — every workflow stays directly runnable:
#   uv run --script factory/f_prompt.py "the ask"

# The smallest workflow: one engineer request, one agent. Also the smoke test.
prompt ask agent="planner" *args:
    uv run --script factory/f_prompt.py --agent {{agent}} {{args}} "{{ask}}"

# Settle the spec before committing to work.
plan ask *args:
    uv run --script factory/f_plan.py {{args}} "{{ask}}"

# The plan already exists; only the code is missing.
build ask *args:
    uv run --script factory/f_build.py {{args}} "{{ask}}"

# Small, well-understood work: plan, commit, build, commit.
plan-build ask *args:
    uv run --script factory/f_plan_build.py {{args}} "{{ask}}"

# Code with a suite to satisfy.
build-test ask *args:
    uv run --script factory/f_build_test.py {{args}} "{{ask}}"

# The standard chain. Code lands last, after the suite is green.
plan-build-test ask *args:
    uv run --script factory/f_plan_build_test.py {{args}} "{{ask}}"

# Read-only recon. Nothing changes.
scout ask *args:
    uv run --script factory/f_scout.py {{args}} "{{ask}}"

# Just the deterministic blocks. No agent at all.
quality ask="run the checks" *args:
    uv run --script factory/f_quality.py {{args}} "{{ask}}"

# Correctness against the request matters more than a suite does.
build-review ask *args:
    uv run --script factory/f_build_review.py {{args}} "{{ask}}"

# Write up work already done.
document ask="write up the change" *args:
    uv run --script factory/f_document.py {{args}} "{{ask}}"

# The full chain, for work whose shape is not obvious.
sdlc ask *args:
    uv run --script factory/f_simple_sdlc.py {{args}} "{{ask}}"

# Prove the whole spine end to end against a real model.
smoke:
    uv run --script factory/f_prompt.py --agent scout \
      "Name the file that owns git plumbing. Where: factory/f_modules/. Done means: one filename. Out of scope: reading it."

# --- trace queries ----------------------------------------------------------------------
# There is no `sqlite3` CLI on this machine, so §12.4's "SQL one-liners" are Python one-liners
# reading through TraceReader. `kill` needs pid-safety logic and belongs with the installer.
#
# Debugging a hang has a fixed order:
#   phases  — where did it stop
#   procs   — what is still alive
#   kill    — verify the stored command matches the pid BEFORE signalling; pids are recycled.

db := "factory/f_data/sssf.db"

# Where the factory itself lives — needed only by `obs`, whose app is deliberately not stamped.
# `install.py` rewrites the fallback below to the tree it stamped from, so a stamped repo needs no
# configuration; FACTORY_HOME still wins.
factory_home := env_var_or_default("FACTORY_HOME", "/home/wa11gf/01-utils/factory2")

# List every run, newest first.
sessions:
    @uv run python -c "from f_modules.tracer import TraceReader as R; \
      [print(f\"{s['f_id']}  {s['status']:<8} {s['engineer']:<10} {s['f_name']}\") \
       for s in R('{{db}}').sessions()]"

# Where did a run stop?
phases f_id:
    @uv run python -c "from f_modules.tracer import TraceReader as R; \
      [print(f\"{p['seq']:>3}  {p['status']:<8} {p['kind']:<9} {p['name']:<18} {p['description']}\") \
       for p in R('{{db}}').phases('{{f_id}}')]"

# The event stream for a run.
tail f_id:
    @uv run python -c "from f_modules.tracer import TraceReader as R; \
      [print(f\"{e['started_at']}  {e['type']:<12} {e['name']:<16} {e['payload_json'][:100]}\") \
       for e in R('{{db}}').poll_events('{{f_id}}')]"

# Children believed alive. Compare the command against the live pid before signalling anything.
procs f_id:
    @uv run python -c "from f_modules.tracer import TraceReader as R; \
      [print(f\"{p['pid']:>8}  {p['name']:<12} {p['command']}\") \
       for p in R('{{db}}').open_processes('{{f_id}}')]"

# Stop a hung run. Verifies the stored command against the live pid before signalling.
kill f_id:
    uv run --script scripts/kill_run.py {{f_id}} --db {{db}}

# Regenerate the PEP 723 dependency block in every factory/f_*.py from pyproject.toml.
# The pyproject list is canonical; this recipe is how the scripts stay in step with it.
deps *args:
    uv run --script scripts/sync_deps.py {{args}}

# Stamp the factory into another repository.
install target *args:
    uv run --script scripts/install.py {{target}} {{args}}

# Scaffold a new workflow. Every generated description must then be replaced.
new-fw name agents:
    uv run --script scripts/make_f.py --name {{name}} --agents {{agents}}

# Copy the roster into a repo. Refuses to overwrite without --force.
config target *args:
    uv run --script scripts/make_config.py {{target}} {{args}}

# Run every test.
test:
    uv run pytest

# §14.4's fifteen checks — the definition of done for a faithful rebuild.
accept:
    uv run pytest tests/test_acceptance.py -v

# --- the trace UI -----------------------------------------------------------------------
# Needs bun. The app lives in `apps/visualizer` and is deliberately NOT stamped into a target
# repo: it is one app that reads any repo's trace db, not a copy per repo. A stamped justfile
# therefore carries the path it was stamped from (`factory_home`, rewritten by install.py), and
# FACTORY_HOME overrides it.
#
# The db path is passed explicitly because the server runs from the app directory and would
# otherwise look for a trace db sitting next to itself.

# Boot the trace UI. Steps to a free port if one is already serving.
obs:
    #!/usr/bin/env bash
    set -euo pipefail
    app="{{factory_home}}/apps/visualizer"
    if [ ! -d "$app" ]; then
        echo "no trace UI at $app — it ships with the factory, not with a stamped repo." >&2
        echo "Point FACTORY_HOME at your factory checkout:  FACTORY_HOME=/path/to/factory2 just obs" >&2
        exit 1
    fi
    if [ ! -f "{{justfile_directory()}}/{{db}}" ]; then
        # The API exits on a missing db and vite would then serve an empty shell, which looks like
        # a broken UI rather than an empty repo.
        echo "no trace db at {{db}} — nothing has run here yet." >&2
        echo "Run something first, e.g.  just scout \"where does X live\"" >&2
        exit 1
    fi
    # A UI left running against another repo owns :4600, and bun would die on a stack trace that
    # reads like the app is broken. Stepping to the next free port is what you would do by hand.
    port="${PORT:-4600}"
    limit=$((port + 20))
    while [ "$port" -lt "$limit" ] && (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
        port=$((port + 1))
    done
    if [ "$port" -ge "$limit" ]; then
        echo "no free port in $((limit - 20))-$limit — something is holding the whole range." >&2
        exit 1
    fi
    [ "$port" = "${PORT:-4600}" ] || echo "[factory] ${PORT:-4600} was busy — using :$port for the api"
    cd "$app"
    bun install
    (SSSF_DB="{{justfile_directory()}}/{{db}}" PORT="$port" bun run server/index.ts &)
    PORT="$port" bunx vite

# Any read-only query against the trace db: just q "select f_id, status from sessions limit 5"
q sql:
    @uv run python -c "import sqlite3, sys; \
      rows = sqlite3.connect('file:{{db}}?mode=ro', uri=True).execute('''{{sql}}''').fetchall(); \
      [print('  '.join(str(c) for c in r)) for r in rows]"

# Two cheap read-only runs, end to end — the first thing to try in a fresh repo.
demo:
    @echo "1/2  f_prompt: one agent, one prompt"
    just prompt "reply with a one-line summary of this repo" scout
    @echo "\n2/2  f_scout: read-only recon"
    just scout "list the top-level directories in this repo and what each is for. change nothing."
    @echo "\nboth done. now run:  just sessions    (or: just obs)"
