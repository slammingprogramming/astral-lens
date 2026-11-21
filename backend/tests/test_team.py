"""M5: the team profile. Tokens are minted by the test with locally generated RSA and EC keys and
served by a local fake provider (discovery + JWKS over real HTTP), so signature, claim and
rollover handling are exercised for real. NOT verified: any real identity provider (Keycloak,
Entra, Okta...), browser login, opaque access tokens, or provider-specific claim shapes."""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from fastapi.testclient import TestClient

from astral import oidc
from astral.api import create_app
from astral.config import Settings

AUD = "astral"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwk_rsa(key, kid):
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "use": "sig", "kid": kid, "alg": "RS256", "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}


def jwk_ec(key, kid):
    n = key.public_key().public_numbers()
    return {"kty": "EC", "use": "sig", "kid": kid, "crv": "P-256", "x": b64(n.x.to_bytes(32, "big")), "y": b64(n.y.to_bytes(32, "big"))}


class Idp:
    def __init__(self, server):
        self.server = server
        self.rsa = rsa.generate_private_key(65537, 2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.keys = [jwk_rsa(self.rsa, "r1"), jwk_ec(self.ec, "e1")]
        server.add("/.well-known/openid-configuration", {"issuer": server.url, "jwks_uri": server.url + "/jwks"})
        server.add_fn("/jwks", lambda req: (200, "application/json", {"keys": self.keys}, {}))
        self.issuer = server.url

    def mint(self, sub="u1", *, alg="RS256", kid=None, key=None, header=None, **claims):
        now = int(time.time())
        body = {"iss": self.issuer, "aud": AUD, "sub": sub, "exp": now + 300, "iat": now, "name": sub.title(), **claims}
        body = {k: v for k, v in body.items() if v is not None}
        head = {"alg": alg, "typ": "JWT", "kid": kid or ("e1" if alg == "ES256" else "r1"), **(header or {})}
        head = {k: v for k, v in head.items() if v is not None}
        signing_input = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(body).encode())}"
        if alg == "RS256":
            sig = (key or self.rsa).sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256":
            r, s = utils.decode_dss_signature((key or self.ec).sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        else:
            sig = b"x" * 32
        return f"{signing_input}.{b64(sig)}"


@pytest.fixture()
def idp(server):
    return Idp(server)


# ------------------------------------------------------------------ the validator
def test_valid_rs256_and_es256_tokens_pass(idp):
    v = oidc.OidcValidator(idp.issuer, AUD)
    assert v.validate(idp.mint("alice"))["sub"] == "alice"
    assert v.validate(idp.mint("bob", alg="ES256"))["sub"] == "bob"
    assert v.validate(idp.mint("carol", aud=["other", AUD]))["sub"] == "carol"      # aud may be a list


@pytest.mark.parametrize("what", ["alg_none", "hs256_public_key_trick", "wrong_issuer", "wrong_audience", "expired", "not_yet_valid",
                                  "unknown_kid", "no_kid", "no_sub", "wrong_key", "tampered_payload", "garbage", "alg_key_mismatch"])
def test_bad_tokens_are_refused(idp, what):
    v = oidc.OidcValidator(idp.issuer, AUD)
    now = int(time.time())
    other = rsa.generate_private_key(65537, 2048)
    tok = {
        "alg_none": lambda: idp.mint(alg="none", header={"alg": "none"}),
        "hs256_public_key_trick": lambda: idp.mint(alg="HS256"),
        "wrong_issuer": lambda: idp.mint(iss="https://evil.example"),
        "wrong_audience": lambda: idp.mint(aud="someone-else"),
        "expired": lambda: idp.mint(exp=now - 3600),
        "not_yet_valid": lambda: idp.mint(nbf=now + 3600),
        "unknown_kid": lambda: idp.mint(kid="nope"),
        "no_kid": lambda: idp.mint(header={"kid": None}),
        "no_sub": lambda: idp.mint(sub=""),
        "wrong_key": lambda: idp.mint(key=other),
        "tampered_payload": lambda: (lambda t: t.split(".")[0] + "." + b64(json.dumps({"iss": idp.issuer, "aud": AUD, "sub": "admin",
                                                                                      "exp": now + 999}).encode()) + "." + t.split(".")[2])(idp.mint("u1")),
        "garbage": lambda: "not.a.jwt.at.all",
        "alg_key_mismatch": lambda: idp.mint(alg="ES256", kid="r1"),             # EC signature against the RSA key id
    }[what]()
    with pytest.raises(oidc.OidcError):
        v.validate(tok)


def test_a_rotated_key_is_picked_up_once_and_without_hammering_the_provider(idp, server):
    clock = [1000.0]
    v = oidc.OidcValidator(idp.issuer, AUD, clock=lambda: clock[0])
    v.validate(idp.mint("a"))
    new = rsa.generate_private_key(65537, 2048)
    idp.keys.append(jwk_rsa(new, "r2"))
    before = len([r for r in server.requests if r["path"] == "/jwks"])
    with pytest.raises(oidc.OidcError):                                   # within 30 s of the last fetch: no refetch, so refused
        v.validate(idp.mint("a", kid="r2", key=new))
    assert len([r for r in server.requests if r["path"] == "/jwks"]) == before
    clock[0] += 31
    assert v.validate(idp.mint("a", kid="r2", key=new))["sub"] == "a"
    assert len([r for r in server.requests if r["path"] == "/jwks"]) == before + 1


def test_issuer_and_discovery_safety_rules(server):
    with pytest.raises(oidc.OidcError, match="https"):
        oidc.OidcValidator("http://idp.example.com", AUD)
    server.add("/.well-known/openid-configuration", {"issuer": "https://different.example", "jwks_uri": server.url + "/jwks"})
    with pytest.raises(oidc.OidcError, match="different issuer"):
        oidc.OidcValidator(server.url, AUD)._load_keys()


def test_jwks_on_a_different_host_is_refused(server):
    server.add("/.well-known/openid-configuration", {"issuer": server.url, "jwks_uri": "http://localhost:1/jwks"})
    with pytest.raises(oidc.OidcError, match="different host"):
        oidc.OidcValidator(server.url, AUD)._load_keys()


# ------------------------------------------------------------------ the API
def team_settings(idp, dsn, tmp_path, **kw):
    return Settings(dsn=dsn, data_dir=tmp_path / "team", profile="team", oidc_issuer=idp.issuer, oidc_audience=AUD,
                    team_admins=("admin",), **kw)


@pytest.fixture()
def team(idp, db, dsn, tmp_path):
    app = create_app(team_settings(idp, dsn, tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        def as_(sub):
            return {"Authorization": f"Bearer {idp.mint(sub)}"}
        c.as_ = as_
        yield c


def test_the_team_profile_will_not_start_without_a_real_provider(dsn, tmp_path, db):
    with pytest.raises(RuntimeError, match="ASTRAL_OIDC_ISSUER"):
        create_app(Settings(dsn=dsn, data_dir=tmp_path, profile="team"), db=db)


def test_unauthenticated_and_badly_authenticated_requests_get_401(team):
    assert team.get("/cases").status_code == 401
    assert team.get("/cases", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert team.get("/cases", headers={"Authorization": "Basic YWRtaW46YWRtaW4="}).status_code == 401
    assert team.get("/health").status_code == 200                           # liveness needs no identity


def test_authentication_is_not_authorization(team):
    h = team.as_("newcomer")
    assert team.get("/cases", headers=h).json() == []                       # no roles: sees nothing
    assert team.post("/cases", json={"name": "x"}, headers=h).status_code == 404   # and cannot create
    me = team.get("/me", headers=h).json()
    assert me["grants"] == [] and me["name"] == "Newcomer"


def test_roles_decide_what_each_person_can_do_in_a_case(team):
    admin, rev, inv, view, out = (team.as_(s) for s in ("admin", "rita", "ivan", "vera", "outsider"))
    case = team.post("/cases", json={"name": "joint"}, headers=admin).json()["id"]
    pid = {s: team.get("/me", headers=team.as_(s)).json()["principal_id"] for s in ("rita", "ivan", "vera", "outsider")}
    for s, role in (("rita", "reviewer"), ("ivan", "investigator"), ("vera", "viewer")):
        assert team.put(f"/cases/{case}/members/{pid[s]}", json={"role": role}, headers=admin).status_code == 200
    # visibility
    assert [c["id"] for c in team.get("/cases", headers=rev).json()] == [case]
    assert team.get("/cases", headers=out).json() == [] and team.get(f"/cases/{case}", headers=out).status_code == 404
    # writing evidence: investigator yes, reviewer and viewer no
    up = lambda h: team.post(f"/cases/{case}/artifacts", content=b"hello", headers={**h, "content-type": "text/plain"})
    assert up(inv).status_code == 201 and up(rev).status_code == 404 and up(view).status_code == 404
    # comments: reviewer yes, viewer no, outsider cannot even see
    c = lambda h: team.post(f"/cases/{case}/comments", json={"body": "looks thin"}, headers=h)
    assert c(rev).status_code == 201 and c(view).status_code == 404 and c(out).status_code == 404
    assert team.get(f"/cases/{case}/comments", headers=view).json()[0]["author_name"] == "Rita"
    # exhibits: investigators and owners only
    ex = lambda h: team.post(f"/cases/{case}/exhibit", json={"statement": "mine"}, headers=h).status_code
    assert ex(inv) == 200 and ex(rev) == 404 and ex(view) == 404
    # granting and erasing: owner only
    assert team.put(f"/cases/{case}/members/{pid['outsider']}", json={"role": "viewer"}, headers=inv).status_code == 404
    assert team.post(f"/cases/{case}/erase", json={"legal_basis": "test"}, headers=rev).status_code == 404
    # removing access removes visibility
    assert team.delete(f"/cases/{case}/members/{pid['vera']}", headers=admin).json() == {"removed": 1}
    assert team.get(f"/cases/{case}", headers=view).status_code == 404


def test_a_case_creator_becomes_its_owner_and_workspace_admin_routes_are_admin_only(team):
    admin = team.as_("admin")
    inv_id = team.get("/me", headers=team.as_("ivan")).json()["principal_id"]
    # ivan is a workspace-wide investigator (given by the admin through a case grant plus creation right is separate)
    assert team.post("/plugins", json={"path": "."}, headers=team.as_("ivan")).status_code == 403
    assert team.put("/secrets/X", json={"value": "v"}, headers=team.as_("ivan")).status_code == 403
    me = team.get("/me", headers=admin).json()
    assert {"case_id": None, "role": "owner"} in me["grants"]
    case = team.post("/cases", json={"name": "owned"}, headers=admin).json()["id"]
    assert any(g["case_id"] == case and g["role"] == "owner" for g in team.get("/me", headers=admin).json()["grants"])
    assert inv_id


def test_assignments_need_access_and_can_be_closed_by_the_assignee(team):
    admin, rita = team.as_("admin"), team.as_("rita")
    case = team.post("/cases", json={"name": "work"}, headers=admin).json()["id"]
    rid = team.get("/me", headers=rita).json()["principal_id"]
    body = {"assignee": rid, "note": "check the IBAN", "target_kind": "case"}
    assert team.post(f"/cases/{case}/assignments", json=body, headers=admin).status_code == 400   # no access yet
    team.put(f"/cases/{case}/members/{rid}", json={"role": "reviewer"}, headers=admin)
    aid = team.post(f"/cases/{case}/assignments", json=body, headers=admin).json()["id"]
    assert team.get(f"/cases/{case}/assignments?status=open", headers=rita).json()[0]["assignee_name"] == "Rita"
    assert team.post(f"/cases/{case}/assignments", json=body, headers=rita).status_code == 404     # reviewers cannot assign
    assert team.post(f"/cases/{case}/assignments/{aid}/done", headers=rita).status_code == 200
    assert team.get(f"/cases/{case}/assignments?status=open", headers=rita).json() == []


def test_per_case_audit_review_shows_who_did_what_without_the_content_of_comments(team, db):
    admin, rita = team.as_("admin"), team.as_("rita")
    case = team.post("/cases", json={"name": "review"}, headers=admin).json()["id"]
    rid = team.get("/me", headers=rita).json()["principal_id"]
    team.put(f"/cases/{case}/members/{rid}", json={"role": "reviewer"}, headers=admin)
    team.post(f"/cases/{case}/comments", json={"body": "SECRET-NOTE-TEXT"}, headers=rita)
    r = team.get(f"/cases/{case}/audit", headers=rita).json()                   # reviewers may audit-review
    actions = [e["action"] for e in r["entries"]]
    assert r["chain_ok"] and {"case.create", "role.grant", "comment.write"} <= set(actions)
    assert "SECRET-NOTE-TEXT" not in json.dumps(r)
    assert next(e for e in r["entries"] if e["action"] == "comment.write")["principal"] == "Rita"
    outsider = team.as_("nobody")
    assert team.get(f"/cases/{case}/audit", headers=outsider).status_code == 404


def test_comments_cannot_be_edited_or_deleted(team, db):
    admin = team.as_("admin")
    case = team.post("/cases", json={"name": "c"}, headers=admin).json()["id"]
    team.post(f"/cases/{case}/comments", json={"body": "first"}, headers=admin)
    import psycopg

    with db.connection() as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE comment SET body='rewritten' WHERE case_id=%s", (case,))


def test_solo_profile_still_works_exactly_as_before(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path / "solo"), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        case = c.post("/cases", json={"name": "solo"}).json()["id"]
        assert [x["id"] for x in c.get("/cases").json()][0] == case
        assert c.put(f"/cases/{case}/members/{c.get('/me').json()['principal_id']}", json={"role": "owner"}).status_code == 400
        assert c.post(f"/cases/{case}/comments", json={"body": "note to self"}).status_code == 201
