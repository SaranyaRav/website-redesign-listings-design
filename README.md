# Website redesign pipeline + business listings system — design & prototype

Take-home submission for AV Infotechnologies.

| Path | What |
|---|---|
| [docs/design.md](docs/design.md) | The design document (also exported as [docs/design.pdf](docs/design.pdf)) |
| [docs/diagrams/](docs/diagrams/) | Mermaid sources and rendered PNGs: two architecture diagrams, two data models |
| [prototype/](prototype/) | cPanel deploy-with-rollback prototype against a mock UAPI, with tests ([prototype/README.md](prototype/README.md)) |
| [docs/interview-notes.md](docs/interview-notes.md) | One-line answers to likely follow-up questions (not in the PDF) |

## Assumptions in three lines

1. Clients give written authorization and a scoped **cPanel API token** (never a password); hosts vary, so the deployer probes for SSH/SFTP and falls back to UAPI over HTTPS.
2. "Thousands" means ~5,000 single-location SMBs in year one; an LLM API and an Azure subscription exist; one LLM provider, pinned model, JSON-schema outputs.
3. Directories are reached only through channels their terms allow (official API, approved partner/aggregator, bulk import, or a human in a real browser); **no CAPTCHA solvers, proxies or fingerprinting**.

## Rebuilding the PDF

```bash
cd docs
npx -y @mermaid-js/mermaid-cli -i diagrams/part1_pipeline.mmd -o diagrams/part1_pipeline.png -s 2 -b white
# (same for part2_listings, part1_data_model, part2_data_model)
python tools/md_to_html.py design.md design.html
node tools/html_to_pdf.js design.html design.pdf
```

## Running the prototype tests

```bash
cd prototype
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m pytest -v
```
