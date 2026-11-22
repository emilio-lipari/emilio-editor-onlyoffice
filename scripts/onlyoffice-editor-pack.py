#!/usr/bin/env python3
"""onlyoffice-editor-pack.py — il pacchetto dell'editor ONLYOFFICE per Emilio Deck (PPT-02, WV-PPT-02-01-01).

Dal sito statico dell'immagine `ghcr.io/ranuts/document` (pinnata per digest, estratta con `docker cp`) produce lo zip
che Emilio scarica al primo uso dal server interno, con `EMILIO-MANIFEST.json`: elenco chiuso di file con dimensione e
SHA-256, che il BE usa sia all'installazione sia a ogni file servito (`BE/src/service/deck_editor.rs`).

Che cosa fa, in ordine:
1. legge le regole (`scripts/onlyoffice-editor-pack.rules.json`): file da togliere (AI locale, mappe, Visio) e
   sostituzioni di testo; **ogni regola deve agire**, altrimenti si ferma: l'immagine è cambiata e le regole vanno
   riviste a mano, non indovinate;
2. toglie i `<meta http-equiv="origin-trial">` dalle pagine (l'Origin-Trial WebMCP è legato al dominio di ranuts) e
   decomprime i file che il server dell'immagine manda con `Content-Encoding` (regola `decomprimi_gzip`): il
   protocollo di Emilio serve i byte così come sono, quindi il file deve arrivare già in chiaro;
3. controlla che ogni percorso rispetti la forma che il BE accetta (`deck_editor_policy::percorso_relativo_ammesso`,
   riscritta qui e provata con `--self-test`): un file che il BE rifiuterebbe sarebbe un 404 a runtime; e che due
   percorsi non coincidano ignorando maiuscole e minuscole (Windows e macOS li scriverebbero sullo stesso file: con il
   vecchio nome `MANIFEST.json` il `manifest.json` del sito fermava l'installazione su Windows, 2026-11-10);
4. scrive lo zip in modo **deterministico** (ordine dei nomi, data fissa): stesso sito, stesse regole, stessi byte;
5. stampa impronta e dimensione del pacchetto e l'impronta del manifest; con `--scrivi-codice` le scrive in
   `EDITOR_PACK_SHA256`, `EDITOR_PACK_BYTES` ed `EDITOR_MANIFEST_SHA256` di `deck_editor.rs` (una sola sede: il BE
   rifiuta un manifest con un'altra impronta); con `--check` verifica che il codice e il pacchetto coincidano.

Nessuna dipendenza: Python standard. Il codice dell'editor è di terzi (AGPL): il pacchetto porta `LICENSE` e le note
se il sito le contiene, e il manifest elenca le modifiche fatte (`changes`).

Uso:
    python3 scripts/onlyoffice-editor-pack.py --sorgente SITO --uscita DIR --digest-immagine sha256:… [--scrivi-codice]
    python3 scripts/onlyoffice-editor-pack.py --check DIR/editor-onlyoffice.zip
    python3 scripts/onlyoffice-editor-pack.py --self-test
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
CODICE = RADICE / "BE" / "src" / "service" / "deck_editor.rs"
REGOLE = Path(__file__).resolve().with_suffix(".rules.json")
NOME_PACCHETTO = "editor-onlyoffice.zip"
MANIFEST = "EMILIO-MANIFEST.json"
DATA_FISSA = (1980, 1, 1, 0, 0, 0)

# Gli stessi tetti di deck_editor_policy.rs.
MAX_LUNGHEZZA = 400
MAX_PROFONDITA = 24
MAX_FILE = 20_000
MAX_BYTE_PER_FILE = 512 * 1024 * 1024
MAX_BYTE_ESTRATTI = 2 * 1024 * 1024 * 1024
VIETATI = set('\\:%*?#"<>|')

ORIGIN_TRIAL = re.compile(rb"<meta\s+http-equiv=[\"']origin-trial[\"'][^>]*>\s*", re.IGNORECASE)


class Fermo(Exception):
    """Errore che ferma lo script con un messaggio per chi lo lancia."""


def percorso_ammesso(p: str) -> bool:
    """La stessa regola di `deck_editor_policy::percorso_relativo_ammesso`."""
    if not p or len(p.encode("utf-8")) > MAX_LUNGHEZZA:
        return False
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 or c in VIETATI for c in p):
        return False
    segmenti = p.split("/")
    if len(segmenti) > MAX_PROFONDITA:
        return False
    return all(s and s not in (".", "..") and not s.endswith((".", " ")) and not s.startswith(" ") for s in segmenti)


def leggi_regole(percorso: Path) -> dict:
    regole = json.loads(percorso.read_text(encoding="utf-8"))
    for chiave in ("togli", "sostituzioni"):
        if not isinstance(regole.get(chiave), list):
            raise Fermo(f"regole: '{chiave}' deve essere un elenco")
    return regole


def raccogli(sorgente: Path) -> dict[str, Path]:
    """I file del sito, per percorso relativo con `/`. Un link simbolico ferma tutto."""
    file: dict[str, Path] = {}
    for cartella, sottocartelle, nomi in os.walk(sorgente, followlinks=False):
        for nome in list(sottocartelle) + nomi:
            p = Path(cartella) / nome
            if p.is_symlink():
                raise Fermo(f"link simbolico nel sito: {p.relative_to(sorgente)}")
        for nome in nomi:
            p = Path(cartella) / nome
            rel = p.relative_to(sorgente).as_posix()
            file[rel] = p
    return file


def corrisponde(percorso: str, glob: str) -> bool:
    """`fnmatch` con `**/` iniziale che vale anche zero cartelle (`**/wllama/**` prende `wllama/x`). Come in
    `fnmatch`, `*` attraversa anche `/`."""
    if fnmatch.fnmatchcase(percorso, glob):
        return True
    while glob.startswith("**/"):
        glob = glob[3:]
        if fnmatch.fnmatchcase(percorso, glob):
            return True
    return False


def applica_togli(file: dict[str, Path], regole: dict) -> list[str]:
    cambi = []
    for r in regole["togli"]:
        glob = r["glob"]
        tolti = [k for k in file if corrisponde(k, glob)]
        if not tolti:
            raise Fermo(f"la regola 'togli' {glob!r} non toglie niente: l'immagine è cambiata, rivedi le regole")
        for k in tolti:
            del file[k]
        cambi.append(f"tolti {len(tolti)} file: {glob} ({r.get('motivo', '')})")
    return cambi


def contenuti(file: dict[str, Path], regole: dict) -> tuple[dict[str, bytes], list[str]]:
    """I byte di ogni file dopo le sostituzioni e la rimozione dell'Origin-Trial."""
    dati = {k: p.read_bytes() for k, p in file.items()}
    cambi = []
    for s in regole["sostituzioni"]:
        glob, vecchio, nuovo = s["file"], s["vecchio"].encode("utf-8"), s["nuovo"].encode("utf-8")
        trovati = [(k, dati[k].count(vecchio)) for k in sorted(dati) if corrisponde(k, glob)]
        totale = sum(n for _, n in trovati)
        if totale != 1:
            raise Fermo(f"la sostituzione su {glob!r} trova il testo {totale} volte (serve esattamente 1)")
        k = next(k for k, n in trovati if n == 1)
        dati[k] = dati[k].replace(vecchio, nuovo)
        cambi.append(f"sostituito in {k}: {s.get('motivo', '')}")
    for r in regole.get("decomprimi_gzip", []):
        k, inizio = r["file"], bytes.fromhex(r["inizio"])
        if k not in dati:
            raise Fermo(f"la regola 'decomprimi_gzip' cerca {k!r}, che non c'è: l'immagine è cambiata, rivedi le regole")
        if dati[k][:2] != b"\x1f\x8b":
            raise Fermo(f"{k} non è compresso con gzip: l'immagine è cambiata, rivedi le regole")
        with gzip.GzipFile(fileobj=io.BytesIO(dati[k])) as g:
            chiaro = g.read(MAX_BYTE_PER_FILE + 1)
        if len(chiaro) > MAX_BYTE_PER_FILE:
            raise Fermo(f"{k} decompresso supera {MAX_BYTE_PER_FILE} byte")
        if not chiaro.startswith(inizio):
            raise Fermo(f"{k} decompresso non comincia con {r['inizio']}: rivedi la regola")
        cambi.append(f"decompresso {k} ({len(dati[k])} → {len(chiaro)} byte): {r.get('motivo', '')}")
        dati[k] = chiaro
    if regole.get("togli_origin_trial", True):
        n = 0
        for k in sorted(dati):
            if k.endswith((".html", ".htm")):
                nuovo, quanti = ORIGIN_TRIAL.subn(b"", dati[k])
                if quanti:
                    dati[k] = nuovo
                    n += quanti
        cambi.append(f"tolti {n} meta origin-trial")
    return dati, cambi


def scegli_ingresso(dati: dict[str, bytes], richiesto: str | None) -> str:
    if richiesto:
        if richiesto not in dati:
            raise Fermo(f"l'ingresso {richiesto!r} non è nel sito")
        return richiesto
    for candidato in ("editor/index.html", "editor.html", "index.html"):
        if candidato in dati:
            return candidato
    raise Fermo("nessun ingresso trovato (editor/index.html, editor.html, index.html): usa --ingresso")


def collisioni_senza_maiuscole(percorsi) -> list[tuple[str, str]]:
    """Le coppie che su un filesystem che non distingue le maiuscole finirebbero sullo stesso file, o su un file e una
    cartella con lo stesso nome; il manifest del pacchetto conta come un percorso. Stessa regola di
    `deck_editor_policy::collisione_senza_maiuscole` (che usa `to_lowercase`, qui `lower`)."""
    visti = {MANIFEST.lower(): MANIFEST}
    coppie = []
    ordinati = sorted(percorsi)
    for p in ordinati:
        k = p.lower()
        if k in visti:
            coppie.append((visti[k], p))
        else:
            visti[k] = p
    for p in ordinati:
        parti = p.split("/")
        for i in range(1, len(parti)):
            cartella = "/".join(parti[:i]).lower()
            if cartella in visti:
                coppie.append((visti[cartella], p))
                break
    return coppie


def controlla(dati: dict[str, bytes]) -> None:
    cattivi = sorted(k for k in dati if not percorso_ammesso(k) or k == MANIFEST)
    if cattivi:
        raise Fermo("percorsi che il BE rifiuterebbe: " + ", ".join(cattivi[:10]))
    doppi = collisioni_senza_maiuscole(dati)
    if doppi:
        raise Fermo("percorsi che su Windows e macOS coincidono: " + ", ".join(f"{a} ~ {b}" for a, b in doppi[:10]))
    if len(dati) > MAX_FILE:
        raise Fermo(f"troppi file: {len(dati)} (massimo {MAX_FILE})")
    grandi = [k for k, b in dati.items() if len(b) > MAX_BYTE_PER_FILE]
    if grandi:
        raise Fermo("file oltre il tetto: " + ", ".join(grandi))
    if sum(len(b) for b in dati.values()) > MAX_BYTE_ESTRATTI:
        raise Fermo("il sito estratto supera il tetto totale")


def manifest(dati: dict[str, bytes], versione: str, ingresso: str, digest: str, cambi: list[str]) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", versione):
        raise Fermo(f"versione non valida: {versione!r}")
    m = {
        "schema": 1,
        "version": versione,
        "entry": ingresso,
        "upstream": {"image": "ghcr.io/ranuts/document", "digest": digest},
        "changes": cambi,
        "files": {k: {"size": len(dati[k]), "sha256": hashlib.sha256(dati[k]).hexdigest()} for k in sorted(dati)},
    }
    return (json.dumps(m, ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode("utf-8")


def scrivi_zip(dati: dict[str, bytes], testo_manifest: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for nome, corpo in [(MANIFEST, testo_manifest)] + [(k, dati[k]) for k in sorted(dati)]:
            info = zipfile.ZipInfo(nome, date_time=DATA_FISSA)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, corpo)
    return buf.getvalue()


def costruisci(sorgente: Path, regole: dict, versione: str, digest: str, ingresso: str | None) -> tuple[bytes, dict]:
    if not sorgente.is_dir():
        raise Fermo(f"sorgente non trovata: {sorgente}")
    file = raccogli(sorgente)
    cambi = applica_togli(file, regole)
    dati, altri = contenuti(file, regole)
    cambi += altri
    controlla(dati)
    scelto = scegli_ingresso(dati, ingresso)
    testo = manifest(dati, versione, scelto, digest, cambi)
    pacchetto = scrivi_zip(dati, testo)
    info = {"file": len(dati), "ingresso": scelto, "cambi": cambi, "manifest_sha256": hashlib.sha256(testo).hexdigest()}
    return pacchetto, info


def costanti_nel_codice(codice: str) -> tuple[str, int, str]:
    sha = re.findall(r'^pub const EDITOR_PACK_SHA256: &str = "([0-9a-f]*)";$', codice, re.M)
    byte = re.findall(r"^pub const EDITOR_PACK_BYTES: u64 = (\d+);$", codice, re.M)
    man = re.findall(r'^pub const EDITOR_MANIFEST_SHA256: &str = "([0-9a-f]*)";$', codice, re.M)
    if len(sha) != 1 or len(byte) != 1 or len(man) != 1:
        raise Fermo("in deck_editor.rs EDITOR_PACK_SHA256, EDITOR_PACK_BYTES ed EDITOR_MANIFEST_SHA256 devono comparire una volta")
    return sha[0], int(byte[0]), man[0]


def scrivi_costanti(codice: str, sha: str, dimensione: int, manifest_sha: str) -> str:
    costanti_nel_codice(codice)
    codice = re.sub(r'^(pub const EDITOR_PACK_SHA256: &str = )"[0-9a-f]*";$', rf'\1"{sha}";', codice, count=1, flags=re.M)
    codice = re.sub(r"^(pub const EDITOR_PACK_BYTES: u64 = )\d+;$", rf"\g<1>{dimensione};", codice, count=1, flags=re.M)
    return re.sub(
        r'^(pub const EDITOR_MANIFEST_SHA256: &str = )"[0-9a-f]*";$', rf'\1"{manifest_sha}";', codice, count=1, flags=re.M
    )


def self_test() -> None:
    assert percorso_ammesso("sdkjs/slide/sdk-all.js")
    for p in ["", "../x", "a//b", "a/./b", "a\\b", "C:x", "a%20b", "a#b", "a?b", "dir /x", "x.", " x", "a\x01b"]:
        assert not percorso_ammesso(p), p
    assert not percorso_ammesso("/".join(["a"] * 25))
    assert corrisponde("wllama/m.bin", "**/wllama/**") and corrisponde("a/b/wllama/m.bin", "**/wllama/**")
    assert not corrisponde("wllamax/m.bin", "**/wllama/**")
    with tempfile.TemporaryDirectory() as d:
        sito = Path(d) / "sito"
        for rel, corpo in {
            "editor/index.html": b'<html><head><meta http-equiv="origin-trial" content="ABC"><script src="../sdk.js"></script></head></html>',
            "sdk.js": b"function Te(){return attendi(15000)}",
            "wllama/modello.bin": b"x" * 10,
            "sdkjs/a.js.map": b"{}",
            "web-apps/apps/visioeditor/v.js": b"v",
            "LICENSE.txt": b"AGPL",
            "wasm/x.wasm.br": gzip.compress(b"\x00asm\x01\x00\x00\x00", mtime=0),
        }.items():
            (sito / rel).parent.mkdir(parents=True, exist_ok=True)
            (sito / rel).write_bytes(corpo)
        regole = {
            "togli": [{"glob": "**/wllama/**"}, {"glob": "**/*.map"}, {"glob": "web-apps/apps/visioeditor/**"}],
            "sostituzioni": [{"file": "*.js", "vecchio": "attendi(15000)", "nuovo": "Promise.resolve()", "motivo": "sw"}],
            "togli_origin_trial": True,
            "decomprimi_gzip": [{"file": "wasm/x.wasm.br", "inizio": "0061736d", "motivo": "wasm"}],
        }
        primo, info = costruisci(sito, regole, "prova-1", "sha256:0", None)
        secondo, _ = costruisci(sito, regole, "prova-1", "sha256:0", None)
        assert primo == secondo, "deterministico"
        assert info["ingresso"] == "editor/index.html"
        z = zipfile.ZipFile(io.BytesIO(primo))
        nomi = z.namelist()
        assert nomi[0] == MANIFEST and sorted(nomi[1:]) == ["LICENSE.txt", "editor/index.html", "sdk.js", "wasm/x.wasm.br"], nomi
        assert z.read("wasm/x.wasm.br") == b"\x00asm\x01\x00\x00\x00"
        m = json.loads(z.read(MANIFEST))
        assert m["entry"] == "editor/index.html" and m["schema"] == 1
        assert b"origin-trial" not in z.read("editor/index.html")
        assert z.read("sdk.js") == b"function Te(){return Promise.resolve()}"
        for k, v in m["files"].items():
            corpo = z.read(k)
            assert v["size"] == len(corpo) and v["sha256"] == hashlib.sha256(corpo).hexdigest(), k
        # una regola che non agisce ferma tutto
        for rotte in (
            {**regole, "togli": regole["togli"] + [{"glob": "non/esiste/**"}]},
            {**regole, "sostituzioni": [{"file": "*.js", "vecchio": "assente", "nuovo": "x"}]},
            {**regole, "sostituzioni": [{"file": "*", "vecchio": "a", "nuovo": "b"}]},
            {**regole, "decomprimi_gzip": [{"file": "non/esiste.br", "inizio": "00"}]},
            {**regole, "decomprimi_gzip": [{"file": "sdk.js", "inizio": "00"}]},
            {**regole, "decomprimi_gzip": [{"file": "wasm/x.wasm.br", "inizio": "ff"}]},
        ):
            try:
                costruisci(sito, rotte, "prova-1", "sha256:0", None)
                raise AssertionError("doveva fermarsi")
            except Fermo:
                pass
        # un nome che il BE rifiuterebbe ferma tutto
        (sito / "x#y.js").write_bytes(b"1")
        try:
            costruisci(sito, regole, "prova-1", "sha256:0", None)
            raise AssertionError("doveva fermarsi sul nome")
        except Fermo as e:
            assert "x#y.js" in str(e)
        (sito / "x#y.js").unlink()
        # un link simbolico ferma tutto
        os.symlink(sito / "sdk.js", sito / "collegamento.js")
        try:
            costruisci(sito, regole, "prova-1", "sha256:0", None)
            raise AssertionError("doveva fermarsi sul link")
        except Fermo:
            pass
    # nomi che su Windows e macOS coincidono fermano tutto; il web manifest del sito da solo va bene
    controlla({"index.html": b"1", "manifest.json": b"{}"})
    for doppi, atteso in (
        ({"index.html": b"1", "manifest.json": b"{}", "MANIFEST.json": b"{}"}, "MANIFEST.json ~ manifest.json"),
        ({"index.html": b"1", "Emilio-Manifest.json": b"{}"}, f"{MANIFEST} ~ Emilio-Manifest.json"),
        ({"index.html": b"1", "Fonts": b"1", "fonts/a.ttf": b"1"}, "Fonts ~ fonts/a.ttf"),
    ):
        try:
            controlla(doppi)
            raise AssertionError("doveva fermarsi sulle maiuscole")
        except Fermo as e:
            assert atteso in str(e), str(e)
    controlla({"index.html": b"1", "A/x.js": b"1", "a/y.js": b"1"})
    codice = 'x\npub const EDITOR_PACK_SHA256: &str = "";\npub const EDITOR_PACK_BYTES: u64 = 0;\n'
    codice += 'pub const EDITOR_MANIFEST_SHA256: &str = "";\ny\n'
    nuovo = scrivi_costanti(codice, "ab" * 32, 123, "cd" * 32)
    assert costanti_nel_codice(nuovo) == ("ab" * 32, 123, "cd" * 32)
    assert costanti_nel_codice(CODICE.read_text(encoding="utf-8"))[1] >= 0
    assert info["manifest_sha256"] == hashlib.sha256(z.read(MANIFEST)).hexdigest()
    print("self-test ok")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sorgente", type=Path, help="cartella del sito statico estratto dall'immagine")
    ap.add_argument("--uscita", type=Path, help="cartella dove scrivere il pacchetto")
    ap.add_argument("--digest-immagine", default="", help="digest dell'immagine ranuts usata (sha256:…)")
    ap.add_argument("--versione", default="ranuts-9.3.0.140-1", help="versione scritta nel manifest")
    ap.add_argument("--ingresso", default=None, help="pagina dell'editor (predefinito: editor/index.html se c'è)")
    ap.add_argument("--regole", type=Path, default=REGOLE)
    ap.add_argument("--scrivi-codice", action="store_true", help="scrive impronta e dimensione in deck_editor.rs")
    ap.add_argument("--check", type=Path, help="verifica che deck_editor.rs dichiari questo pacchetto")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.self_test:
            self_test()
            return 0
        if a.check:
            corpo = a.check.read_bytes()
            sha, byte, man = costanti_nel_codice(CODICE.read_text(encoding="utf-8"))
            try:
                man_pacchetto = hashlib.sha256(zipfile.ZipFile(io.BytesIO(corpo)).read(MANIFEST)).hexdigest()
            except (zipfile.BadZipFile, KeyError):
                man_pacchetto = ""
            atteso = (hashlib.sha256(corpo).hexdigest(), len(corpo), man_pacchetto)
            if (sha, byte, man) != atteso:
                print(f"il codice dichiara {sha or '(vuota)'} / {byte} B / manifest {man or '(vuota)'}; "
                      f"il pacchetto è {atteso[0]} / {atteso[1]} B / manifest {atteso[2] or '(assente)'}")
                return 1
            print("il codice dichiara questo pacchetto")
            return 0
        if not a.sorgente or not a.uscita:
            ap.error("servono --sorgente e --uscita (oppure --check o --self-test)")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", a.digest_immagine):
            raise Fermo("--digest-immagine deve essere il digest pinnato dell'immagine (sha256:…, 64 cifre)")
        pacchetto, info = costruisci(a.sorgente, leggi_regole(a.regole), a.versione, a.digest_immagine, a.ingresso)
        a.uscita.mkdir(parents=True, exist_ok=True)
        dest = a.uscita / NOME_PACCHETTO
        dest.write_bytes(pacchetto)
        sha = hashlib.sha256(pacchetto).hexdigest()
        print(f"{dest}: {len(pacchetto)} B, {info['file']} file, ingresso {info['ingresso']}")
        print(f"SHA-256 {sha}")
        print(f"SHA-256 del manifest {info['manifest_sha256']}")
        for c in info["cambi"]:
            print(f"  - {c}")
        if a.scrivi_codice:
            nuovo = scrivi_costanti(CODICE.read_text(encoding="utf-8"), sha, len(pacchetto), info["manifest_sha256"])
            CODICE.write_bytes(nuovo.encode("utf-8"))  # LF anche su Windows
            print(f"scritte EDITOR_PACK_SHA256, EDITOR_PACK_BYTES ed EDITOR_MANIFEST_SHA256 in {CODICE.relative_to(RADICE)}")
        return 0
    except Fermo as e:
        print(f"fermo: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
