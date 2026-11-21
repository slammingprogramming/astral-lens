"""Run the API plus the built UI against a throw-away database, with the M1 fixture
registries and one seeded case, so the interface can be looked at in a browser.

    python tests/demo_server.py            # http://127.0.0.1:8765/

The 'registries' are the same hand-written local responses the M1 exit test uses.
Nothing here talks to a real service. Needs the dev Postgres (see conftest.ADMIN_DSN).
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from astral.api import create_app  # noqa: E402
from astral.config import Settings  # noqa: E402
from astral.db import SOLO_ACTOR, Database  # noqa: E402

from conftest import ADMIN_DSN, FixtureServer  # noqa: E402
from test_exit_m1 import ALL, CONNECTORS, REPO, fixtures, vouch_sources  # noqa: E402

PORT = int(os.environ.get("ASTRAL_DEMO_PORT", "8765"))


def main() -> None:
    os.environ["COMPANIES_HOUSE_API_KEY"] = "demo"
    os.environ["OPENSANCTIONS_API_KEY"] = "demo"
    os.environ.pop("OPENCORPORATES_API_TOKEN", None)

    name = f"astral_demo_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    data = Path(tempfile.mkdtemp(prefix="astral-demo-"))

    server = FixtureServer()
    fixtures(server)
    db = Database(dsn).open()
    db.migrate()
    db.bootstrap_solo()
    app = create_app(Settings(dsn=dsn, data_dir=data), db=db)
    try:
        with TestClient(app, headers={"x-astral-client": "1"}) as c:
            core = sorted(p for p in (REPO / "plugins" / "core").iterdir() if (p / "plugin.yaml").exists())
            for d in [CONNECTORS / n for n in ALL] + core:
                r = c.post("/plugins", json={"path": str(d)}).json()
                c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
            vouch_sources(db, SOLO_ACTOR)
            case = c.post("/cases", json={"name": "Acme Holdings (demo)"}).json()["id"]
            cfg = {"connector.sec-edgar": {"contact": "ops@example.org", "www_url": server.url, "data_url": server.url}}
            for p in ("gleif", "companies-house", "opencorporates", "opensanctions", "searxng", "wayback-cdx"):
                cfg[f"connector.{p}"] = {"base_url": server.url}
            c.post(f"/cases/{case}/investigate/name",
                   json={"name": "Acme Holdings Ltd", "country": "GB", "plugin_configs": cfg})
            memo = ("Memo. Jane Q Doe (jane.doe@acme.example) was appointed director of Acme Holdings Ltd on "
                    "1 April 2015. Wire the retainer to IBAN GB82WEST12345698765432. Contact: Acme Holdings, London.")
            up = c.post(f"/cases/{case}/artifacts", content=memo.encode(), headers={"content-type": "text/plain"}).json()
            ing = c.post(f"/cases/{case}/artifacts/{up['sha256']}/ingest")
            if ing.status_code != 200:
                raise SystemExit(f"seed ingest failed: {ing.status_code} {ing.text}")
            c.post(f"/cases/{case}/artifacts", content=b"\x00\x01 scanned page, no text layer",
                   headers={"content-type": "application/octet-stream"})
            # A short synthetic video. The speech recogniser is a STUB returning canned text:
            # no ASR model is installed here, so the transcript is demonstration data only.
            from test_media import StubAsr, make_video, CORE as MEDIA_PLUGINS
            for n in MEDIA_PLUGINS:
                if (REPO / "plugins" / "core" / n / "plugin.yaml").exists() and n not in ("ner", "doc-ingest"):
                    r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)}).json()
                    c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
            app.state.host.backends.register(StubAsr())
            vid = data / "demo.mp4"
            make_video(vid, creation_time="2024-03-05T10:20:30Z")
            vsha = c.post(f"/cases/{case}/artifacts", content=vid.read_bytes(), headers={"content-type": "video/mp4"}).json()["sha256"]
            lfw = os.environ.get("ASTRAL_FACE_TEST_DIR")
            if lfw and Path(lfw).exists():              # real photos only if you point at an LFW folder
                for n in ("media-probe", "faces"):
                    r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)}).json()
                    c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
                for person, k in (("George_W_Bush", 1), ("George_W_Bush", 2), ("Colin_Powell", 1), ("Tony_Blair", 1)):
                    img = (Path(lfw) / person / f"{person}_{k:04d}.jpg").read_bytes()
                    s = c.post(f"/cases/{case}/artifacts", content=img, headers={"content-type": "image/jpeg"}).json()["sha256"]
                    c.post(f"/cases/{case}/artifacts/{s}/ingest")
            ing = c.post(f"/cases/{case}/artifacts/{vsha}/ingest")
            if ing.status_code != 200:
                raise SystemExit(f"seed video ingest failed: {ing.status_code} {ing.text}")
        print(f"\nAstral Lens demo: http://127.0.0.1:{PORT}/   (db {name}, fixtures {server.url})\n", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        db.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
        server.close()


if __name__ == "__main__":
    main()
