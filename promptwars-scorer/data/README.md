# data/

Local, private learning data for the scorer. Nothing here except the examples is committed.

- `labeled_records.json` — submissions with their REAL Hack2Skill scores (created by `promptwars-score record add|import` or the web page's "Real scores" tab).
- `calibration.json` — the fitted correction and its validation report (`promptwars-score calibrate`).
- `challenges.json` — your challenge list for the web page. Copy `challenges.example.json` and paste the exact statements.
- `records_template.csv` — column layout for `promptwars-score record import`.

Other participants' repositories and scores stay on your machine; do not publish them.
