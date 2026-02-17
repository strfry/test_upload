# Escalation Smoke Tests

Ziel: prüfen, ob `escalate_to_human` nach der neuen Prompt-Regel weiterhin sinnvoll ausgelöst wird.

## Fall A: Continue möglich (soll NICHT eskalieren)

Datei: `docs/prompt_cases/escalation_continue_possible.json`

Erwartung:
- `actions` enthält **kein** `escalate_to_human`
- stattdessen z.B. konkrete Anschlussfrage + `send_message`

## Fall B: Konfliktfall (soll eskalieren)

Datei: `docs/prompt_cases/escalation_required_conflict.json`

Erwartung:
- `actions` enthält `escalate_to_human`
- `message.text` leer oder nicht sendend
- `analysis` enthält idealerweise `missing_facts` / `suggested_analysis_keys`

## Ausführung

Request exportieren:

```bash
python scripts/prompt_runner.py \
  --input-json docs/prompt_cases/escalation_continue_possible.json \
  --preview-only \
  --dump-request-json /tmp/hf_case_a.json \
  --print-curl
```

```bash
python scripts/prompt_runner.py \
  --input-json docs/prompt_cases/escalation_required_conflict.json \
  --preview-only \
  --dump-request-json /tmp/hf_case_b.json \
  --print-curl
```

Dann jeweils den ausgegebenen `curl` ausführen und die JSON-Responses vergleichen.

## Erweiterte Fälle

### Fall C: Kein erneutes Fragen nach bekanntem Validator-Kontakt

Datei: `docs/prompt_cases/no_repeat_validator_contact.json`

Erwartung:
- keine erneute Frage nach Kontaktweg/Erreichbarkeit von George
- `send_message` kann enthalten sein, aber mit neuem inhaltlichem Schritt

### Fall D: Fokus auf jüngstes User-Thema

Datei: `docs/prompt_cases/focus_latest_user_topic.json`

Erwartung:
- Antwort fokussiert Fee-Thema (letzte User-Nachricht), nicht primär Registrierung

### Fall E: Sicherheitskonflikt ohne Operator-Direktive

Datei: `docs/prompt_cases/escalation_required_security_without_directive.json`

Erwartung:
- `actions` enthält `escalate_to_human`
- kein `send_message` mit Refusal als Ersatz
