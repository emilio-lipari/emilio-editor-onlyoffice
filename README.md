# Pacchetto dell'editor ONLYOFFICE per Emilio

Questo repository pubblica, nelle **Releases**, il pacchetto `editor-onlyoffice.zip` che l'app desktop Emilio scarica
la prima volta che si apre una presentazione. Il pacchetto contiene l'editor web di ONLYOFFICE (`sdkjs`, `web-apps` e il
convertitore `x2t` compilato in WebAssembly). Emilio lo esegue in locale, dentro la propria finestra, senza un Document
Server e senza rete.

*English summary: this repository hosts, as release assets, a modified build of the ONLYOFFICE web editors used offline
by the Emilio desktop app. It is a derivative work of ONLYOFFICE, licensed under the GNU AGPL v3 (see `LICENSE` and
`NOTICE`). Corresponding sources and the list of modifications are below.*

## Versione pubblicata

| Release | File | SHA-256 | Byte |
|---|---|---|---|
| `ranuts-9.3.0.140-1` | `editor-onlyoffice.zip` | `52177b806a4291a844bc51efca7cf8284305afa0e0a9e439e34926178545b8db` | 197 943 393 |

Emilio verifica l'impronta SHA-256 del pacchetto, quella del manifest (`EMILIO-MANIFEST.json`,
`ab7671c51835ec319972957fdff4ce8e470c19cc489f0f16b808fad43275226d`) e quella di ogni file. Un file diverso da questo
viene rifiutato, da qualunque indirizzo arrivi. Una versione nuova è sempre una release nuova: i file di una release
pubblicata non si sostituiscono.

## Da dove viene

- **Sito di partenza:** l'immagine `ghcr.io/ranuts/document@sha256:5e71d767e15d3c7b056eb7bf75f6f1fedf389095cd463de442c01d384d29336b`
  (cartella `/public`), del progetto [ranuts/document](https://github.com/ranuts/document), AGPL-3.0.
- **Editor ONLYOFFICE contenuto:** 9.3.0, da [ONLYOFFICE/sdkjs](https://github.com/ONLYOFFICE/sdkjs) e
  [ONLYOFFICE/web-apps](https://github.com/ONLYOFFICE/web-apps); il convertitore da
  [ONLYOFFICE/core](https://github.com/ONLYOFFICE/core). Copyright Ascensio System SIA. Dettagli e termini aggiuntivi
  (Sezione 7 dell'AGPL) in `NOTICE`.

## Modifiche

Il pacchetto è prodotto da `scripts/onlyoffice-editor-pack.py` con le regole di `scripts/onlyoffice-editor-pack.rules.json`.
L'elenco completo delle modifiche è nel campo `changes` di `EMILIO-MANIFEST.json`; in sintesi:

- **file tolti:** pesi e worker dei motori di AI locale (wllama, WebLLM), il motore e l'editor Visio, le pagine del sito
  di ranuts, i file di configurazione Cloudflare e per i motori di ricerca;
- **due sostituzioni di codice:**
  - `assets/document-*.js`: niente attesa di 15 s del service worker, che dietro il protocollo locale di Emilio non c'è;
  - `sdkjs/slide/sdk-all.js`: correzione del ricolore delle lettere su WebKitGTK (Linux);
- `sdkjs/common/wasm/x2t/x2t.wasm.br` decompresso, perché Emilio serve i file così come sono;
- tolti i `<meta http-equiv="origin-trial">`, legati al dominio di ranuts.

Lo zip è deterministico: stesso sito di partenza e stesse regole danno gli stessi byte.

Per rifare il pacchetto:

```
docker pull ghcr.io/ranuts/document@sha256:5e71d767e15d3c7b056eb7bf75f6f1fedf389095cd463de442c01d384d29336b
docker create --name sito ghcr.io/ranuts/document@sha256:5e71d767e15d3c7b056eb7bf75f6f1fedf389095cd463de442c01d384d29336b
docker cp sito:/public ./sito && docker rm sito
python3 scripts/onlyoffice-editor-pack.py --sorgente ./sito --uscita ./uscita \
  --digest-immagine sha256:5e71d767e15d3c7b056eb7bf75f6f1fedf389095cd463de442c01d384d29336b
sha256sum ./uscita/editor-onlyoffice.zip
```

Le opzioni `--scrivi-codice`, `--check` e `--self-test` dello script leggono il codice di Emilio e funzionano solo dal
repository di Emilio.

## Licenza e marchi

Il contenuto del pacchetto è un'opera derivata di ONLYOFFICE ed è distribuito sotto la **GNU Affero General Public
License v3** (`LICENSE`), con i termini aggiuntivi riportati in `NOTICE`. ONLYOFFICE è un marchio di Ascensio System
SIA; questo pacchetto non è un prodotto ONLYOFFICE né è approvato da Ascensio System SIA.
