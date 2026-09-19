"""Brief summaries for people who are not data scientists: what the situation is and what to do now.

One summary per view (and one for the whole run, "overview") plus one per object (diagnosis, alarm, data-quality
check, batch, evidence). Everything is a deterministic template filled from the run's artifacts: instant, never a
model call, and available in English, Finnish and Swedish from the small dictionaries in this file.

Shape of a view summary::

    {"view", "language", "verdict": "ok" | "attention" | "problem" | "pending", "headline": str,
     "points": [str, ...], "actions": [{"text", "view", "ref", "ask"}, ...], "source": "template"}

* headline: one sentence, at most 22 words; points: at most 3 sentences of at most 28 words.
* actions: 1-3 clickable next steps. ``view`` (+ ``ref``: an object id such as DIAG-000001 / FLAG-000004 /
  CHK-000012 / B00003 / S07, a row range ``rows:120-180`` or a page section ``section:suspicious``) navigates;
  ``ask`` opens the chat with that question.
* No method names and no statistics vocabulary (see FORBIDDEN); sensors are called by the name in the person's
  file (or the name an operator gave), with the internal short name in brackets.

The detailed output of the app stays where it is; the UI shows it under "Show technical analyses".
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable, Optional

from .plain import _read

VIEWS = ("overview", "understanding", "quality", "monitor", "diagnoses", "assessor", "dataflow", "log", "report")
LANGS = ("en", "fi", "sv")
MAX_HEADLINE_WORDS = 22
MAX_POINT_WORDS = 28
MAX_POINTS = 3
MAX_ACTIONS = 3

# Words a plant operator should never have to read in a summary (checked by tests/test_e_brief.py).
FORBIDDEN = (
    "detector", "detectors", "baseline", "baselines", "PCA", "z-score", "z-scores", "zscore", "sigma", "fold", "folds", "out-of-fold", "threshold",
    "thresholds", "AUROC", "changepoint", "changepoints", "exposure", "severity score", "ensemble", "quantile", "quantiles", "robust",
    "artifact", "artifacts", "egress", "ledger", "inference", "inferences", "hypothesis", "hypotheses", "cluster", "clusters", "autoencoder",
    "isolation forest", "CUSUM", "EWMA", "residual", "residuals", "covariance", "contamination", "p-value", "p-values", "JSON", "schema", "alias", "aliases",
)
FORBIDDEN_RE = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in sorted(FORBIDDEN, key=len, reverse=True)) + r")(?![\w-])", re.I)

# Everyday replacements, applied to any text that comes from the data or from other modules (English only).
_SOFTEN: list[tuple["re.Pattern[str]", str]] = [(re.compile(p, re.I), r) for p, r in (
    (r"\bout-of-fold\b", "held-out"), (r"\bseverity score\b", "seriousness"), (r"\bisolation forest\b", "check method"),
    (r"\b[\w-]*detectors\b", "check methods"), (r"\b[\w-]*detector\b", "check method"), (r"\bbaselines?\b", "normal operation"),
    (r"\bpca\b", "pattern check"), (r"\bz-?scores?\b", "distance from normal"), (r"\bsigma\b", "times the normal spread"),
    (r"\bfolds?\b", "parts"), (r"\bthresholds?\b", "limit"), (r"\bauroc\b", "separation quality"), (r"\bchangepoints?\b", "sudden change"),
    (r"\bexposure\b", "share"), (r"\bensemble\b", "combined check"), (r"\bquantiles?\b", "share"), (r"\brobust\s+", ""), (r"\brobust\b", "sturdy"),
    (r"\bartifacts?\b", "files"), (r"\begress guard\b", "safety check"), (r"\begress\b", "sending data out"), (r"\bledger\b", "list"),
    (r"\binferences?\b", "conclusion"), (r"\bhypothes[ie]s\b", "guess"), (r"\bclusters?\b", "group of related sensors"),
    (r"\bautoencoder\b", "check method"), (r"\bcusum\b", "check method"), (r"\bewma\b", "check method"), (r"\bresiduals?\b", "leftover"),
    (r"\bcovariance\b", "joint movement"), (r"\bcontamination\b", "share of odd rows"), (r"\bp-values?\b", "chance of coincidence"),
    (r"\bjson\b", "file"), (r"\bschema\b", "file layout"), (r"\balias(?:es)?\b", "short name"),
)]


def soften(text: Any) -> str:
    """Replace statistics / method vocabulary in a text that was not written for this module."""
    s = str(text or "")
    for rx, rep in _SOFTEN:
        s = rx.sub(rep, s)
    return re.sub(r"\s{2,}", " ", s).strip()


# =====================================================================================================
# texts
# =====================================================================================================
T: dict[str, dict[str, str]] = {
    "en": {
        "pending.h": "This step has not finished yet.",
        "pending.p": "The summary appears here as soon as the step is done.",
        "pending.a": "Watch the progress of the analysis",
        "failed.h": "The analysis stopped before this step could finish.",
        "failed.p": "Everything that finished before it is still available on the other pages.",
        "failed.a": "See what went wrong",
        "skipped.h": "This step was skipped for this run, so there is nothing to show here.",
        "sure.very": "very sure", "sure.fairly": "fairly sure", "sure.notvery": "not very sure", "sure.unsure": "unsure",
        "sureS.very": "the system is very sure", "sureS.fairly": "the system is fairly sure", "sureS.notvery": "the system is not very sure", "sureS.unsure": "the system is unsure",
        "unit.s": "{n} seconds", "unit.s.1": "second", "unit.min": "{n} minutes", "unit.min.1": "minute", "unit.h": "{n} hours", "unit.h.1": "hour",
        "sensor": "sensor {name}", "and": "and", "rows": "rows {a}–{b}", "rows.1": "row {a}",
        # understanding
        "und.h.ok": "The file was read without problems: {rows} rows from {n} sensors.",
        "und.h.groups": "The file was read without problems: {rows} rows from {n} sensors, split into {g} groups.",
        "und.h.odd": "The file was read, but it does not look like ordinary sensor data. Please check how it was read.",
        "und.p.period": "There is one reading about every {period}.",
        "und.p.noperiod": "The file has no time column, so time is counted in rows.",
        "und.p.excluded": "{k} of {n} sensors are left out of the fault search because they never change or only repeat another sensor.",
        "und.p.excluded.1": "One of {n} sensors is left out of the fault search because it never changes or only repeats another sensor.",
        "und.p.names": "The system does not know what each sensor measures. It only looks at how the values behave.",
        "und.p.unsure": "The system had to guess {k} things about the file. Ask it below what it assumed.",
        "und.p.unsure.1": "The system had to guess one thing about the file. Ask it below what it assumed.",
        "und.a.check": "Check that the columns were read the way you expect",
        "und.a.name": "Tell the system what your sensors measure",
        "und.ask": "What did you assume about my data, and what are you unsure about?",
        # data quality
        "dq.h.ok": "The data is clean: all {n} batches passed the checks.",
        "dq.h.caution": "The data can be used. {k} of {n} batches have smaller problems, mostly {top}.",
        "dq.h.bad": "{k} of {n} batches cannot be trusted. The main reason: {top}.",
        "dq.p.top": "Most common problems: {list}.",
        "dq.p.sensors": "Sensors with the most problems: {list}.",
        "dq.p.setaside": "Rows with data problems are set aside, so they are not mistaken for a fault in the process.",
        "dq.p.batch": "A batch is a block of rows that belong together in time.",
        "dq.a.worst": "Look at the worst batch ({id})",
        "dq.a.check": "See the most serious data problem",
        "dq.a.checks": "See all checks",
        "dq.ask": "Which data problems matter most, and can I still use this data?",
        # monitor
        "mon.h.none": "Nothing unusual was found: the process behaved normally throughout.",
        "mon.h.points": "{n} single readings do not fit their surroundings. No longer-lasting change in the process was found.",
        "mon.h.points.1": "One single reading does not fit its surroundings. No longer-lasting change in the process was found.",
        "mon.h.events": "Unusual behaviour was found in {k} places. The strongest is at {where}, mainly on {sensor}.",
        "mon.h.events.short": "Unusual behaviour was found in {k} places. The strongest is at {where}.",
        "mon.h.events.1": "Unusual behaviour was found in one place: {where}, mainly on {sensor}.",
        "mon.h.events.short.1": "Unusual behaviour was found in one place: {where}.",
        "mon.p.causes": "Of these, {list}.",
        "mon.cause.process": "{n} look like a real change in the process", "mon.cause.sensor": "{n} look like a faulty sensor",
        "mon.cause.data": "{n} look like a data problem", "mon.cause.mixed": "{n} have more than one cause", "mon.cause.unknown": "{n} could not be explained",
        "mon.p.points": "{n} single odd readings were also found. Each is a glitch or a manipulation; the data alone can't tell.",
        "mon.p.points.only": "Each of them is a glitch or a manipulation; the data alone can't tell.",
        "mon.p.groups": "{k} of {g} groups were affected.",
        "mon.p.dq": "{n} further alarms are about problems in the data, not in the process.",
        "mon.p.learned": "The system learned what normal operation looks like from your data itself. Nobody had to mark the faults.",
        "mon.a.top": "Look at the strongest event ({where})",
        "mon.a.sus": "Go through the list of suspicious rows",
        "mon.a.diag": "Read what probably caused it",
        "mon.a.chart": "See the timeline of the whole run",
        "mon.ask": "What happened at {where}, and what should I check?",
        "mon.ask.none": "How do you know that the process was normal?",
        "mon.ask.points": "Are the single odd readings a sensor problem or something worse?",
        # diagnoses
        "dg.h.none": "No fault explanation was needed: nothing unusual enough was found.",
        "dg.h.top": "Most important finding: {what} {where}. {sure}.",
        "dg.h.top.short": "Most important finding: {what} {where}.",
        "dg.h.top.min": "Most important finding: {what}.",
        "dg.what.process": "the process itself probably changed", "dg.what.sensor": "{sensor} is probably faulty", "dg.what.sensor.nosig": "a sensor is probably faulty",
        "dg.what.data": "a problem in the data, not in the process,", "dg.what.mixed": "the process and a sensor both seem involved",
        "dg.what.unknown": "something unusual that the system cannot explain", "dg.what.points": "{n} single odd readings (glitches or manipulation)",
        "dg.where.rows": "at {rows}", "dg.where.group": "in group {g}", "dg.where.both": "in group {g}, {rows}", "dg.where.none": "",
        "dg.p.count": "{n} findings in total: {list}.",
        "dg.one.process": "There is one finding, and it is about the process.", "dg.one.sensor": "There is one finding, and it is about a faulty sensor.", "dg.one.data": "There is one finding, and it is about a data problem.",
        "dg.one.mixed": "There is one finding, with more than one cause.", "dg.one.unknown": "There is one finding, and its cause could not be determined.",
        "dg.count.process": "{n} about the process", "dg.count.sensor": "{n} about faulty sensors", "dg.count.data": "{n} about data problems",
        "dg.count.mixed": "{n} with more than one cause", "dg.count.unknown": "{n} unexplained",
        "dg.p.challenge": "The system double-checked every finding: {list}.", "dg.p.challenge.all": "The system double-checked every finding, and all {n} held up.", "dg.p.challenge.all.1": "The system double-checked the finding, and it held up.",
        "dg.ch.supported": "{n} held up", "dg.ch.weakened": "{n} are shaky", "dg.ch.weakened.1": "1 is shaky", "dg.ch.rejected": "{n} did not hold",
        "dg.p.review": "{k} of {n} have been reviewed by a person so far.",
        "dg.p.review.none": "No person has reviewed them yet. Your accept, question or correction is recorded.",
        "dg.a.open": "Open the most important finding",
        "dg.a.decide": "Accept, question or correct it",
        "dg.ask": "What should I check on site for {id}?",
        # assessor
        "as.h.good": "Your data is good enough for reliable fault finding (overall rating {pct}).",
        "as.h.fair": "Your data is usable, but fault finding would be more reliable with better data (overall rating {pct}).",
        "as.h.weak": "Your data is too weak for reliable fault finding (overall rating {pct}).",
        "as.h.noscore": "The check of how useful your data is has finished.",
        "as.p.more.yes": "More data of the same kind would probably help.", "as.p.more.no": "More data of the same kind would probably not help much.",
        "as.p.more.unclear": "It is unclear whether more data would help.",
        "as.p.less.yes": "Removing the bad rows would probably help.", "as.p.less.no": "Removing rows would probably not help.",
        "as.p.less.unclear": "It is unclear whether removing rows would help.",
        "as.p.thin": "{k} of {n} operating conditions are rarely seen in the data, so faults there are harder to spot.",
        "as.p.recs": "The system has {n} suggestions for improving the data. Nothing is changed until you approve it.",
        "as.a.recs": "See the suggestions for improving the data",
        "as.ask": "Would more data help, and what kind?",
        "as.ask2": "Which rows or sensors should I remove to get better results?",
        # data flow
        "df.h.local": "Your data stayed on this computer. Nothing was sent to the internet.",
        "df.h.sent": "{n} short summaries were sent to an outside AI service. A copy of your measurements never leaves this computer.",
        "df.h.blocked": "Your data stayed on this computer. {b} attempts to contact an outside AI service were stopped.",
        "df.p.local": "The AI helper that words the explanations ran on this computer {n} times.",
        "df.p.nolocal": "No AI helper was used. All texts come from fixed rules inside the program.",
        "df.p.mode.off": "Current setting: outside services are switched off.",
        "df.p.mode.on": "Current setting: an outside service may receive short summaries, never rows of your data.",
        "df.p.list": "Every use of an AI helper is written down and can be inspected.",
        "df.a.list": "See the list of what was sent where",
        "df.a.mode": "Check or change the privacy setting",
        "df.ask": "Did any of my data leave this computer?",
        # decision log
        "lg.h.ok": "Everything the system and people did is recorded: {n} entries, and the record is unchanged.",
        "lg.h.plain": "Everything the system and people did is recorded: {n} entries.",
        "lg.h.bad": "Warning: the record of decisions was changed after the fact, first at entry {seq}.",
        "lg.p.human": "{k} entries are decisions made by people (accept, question, correct).",
        "lg.p.human.none": "No person has made a decision in this run yet.",
        "lg.p.open": "{k} findings still wait for a person's decision.",
        "lg.p.why": "The record cannot be edited without it showing, so it proves who decided what.",
        "lg.a.verify": "Check the record yourself",
        "lg.a.review": "Review the findings that wait for a decision",
        "lg.ask": "Who decided what in this run?",
        # report
        "rp.h.ready": "The report of this run is ready to read, download or send.",
        "rp.h.can": "A report of this run can be made now. It takes a few seconds.",
        "rp.p.formats": "It is available in English, Finnish and Swedish, as a web page, a PDF or slides.",
        "rp.p.content": "It covers how good the data is, what unusual things happened, the likely causes and what is still uncertain.",
        "rp.p.local": "The report is made on this computer.",
        "rp.a.open": "Read the report",
        "rp.a.send": "Send it by e-mail",
        "rp.ask": "Summarise this run in five sentences for my manager.",
        # overview
        "ov.h.running": "The analysis is still running: {done} of {total} steps are finished.",
        "ov.h.failed": "The analysis stopped at the step \"{stage}\". Earlier results are still available.",
        "ov.h": "{data}, and {proc}.",
        "ov.data.ok": "Your data is clean", "ov.data.caution": "Your data is usable", "ov.data.bad": "Parts of your data cannot be trusted", "ov.data.pending": "Your file was read",
        "ov.proc.none": "nothing unusual was found in the process", "ov.proc.points": "{n} single readings look odd", "ov.proc.points.1": "one single reading looks odd",
        "ov.proc.events": "unusual behaviour was found in {k} places", "ov.proc.events.1": "unusual behaviour was found in one place",
        "ov.proc.pending": "the search for unusual behaviour has not finished yet",
        "ov.p.data.ok": "Data: all {n} batches passed the checks.",
        "ov.p.data.caution": "Data: {k} of {n} batches have smaller problems, mostly {top}.",
        "ov.p.data.bad": "Data: {k} of {n} batches cannot be trusted, mainly because of {top}.",
        "ov.p.first": "Look first at this: {what} {where}.",
        "ov.p.first.min": "Look first at this: {what}.",
        "ov.a.diag": "Open the most important finding",
        "ov.a.batch": "Look at the worst batch ({id})",
        "ov.a.sus": "Go through the suspicious rows",
        "ov.a.report": "Read or send the report",
        "ov.a.progress": "Watch the progress of the analysis",
        "ov.ask": "What should I do first?",
        "stage.ingest": "reading the file", "stage.profile": "understanding the sensors", "stage.quality": "checking the data", "stage.detect": "looking for unusual behaviour",
        "stage.diagnose": "explaining the findings", "stage.assess": "rating the data", "stage.report": "writing the report",
        # items
        "it.dg.h.process": "The process itself probably changed here.",
        "it.dg.h.sensor": "{sensor} is probably faulty.", "it.dg.h.sensor.nosig": "A sensor is probably faulty.",
        "it.dg.h.data": "This is probably a problem in the data, not in the process.",
        "it.dg.h.data.sig": "The readings of {sensor} are probably wrong here; the process itself looks fine.",
        "it.dg.h.mixed": "Both the process and a sensor seem to be involved here.",
        "it.dg.h.unknown": "Something unusual happened here, but the system cannot tell whether the process or a sensor caused it.",
        "it.dg.h.points": "{n} single readings do not fit their surroundings: a glitch or a manipulation; the data alone can't tell.",
        "it.p.sure": "How sure: {sure} ({pct}).",
        "it.p.sure.weakened": "How sure: {sure} ({pct}), and its own double-check found weak points.",
        "it.p.sure.rejected": "How sure: {sure} ({pct}). The system's own double-check did not support this, so do not rely on it.",
        "it.p.where": "Where: {where}.", "it.p.where.time": "Where: {where}, from {t0} to {t1}.",
        "it.p.where.points": "Where: {n} places spread over the data; the list of suspicious rows shows each one.",
        "it.where.rows": "{rows}", "it.where.group": "group {g}, {rows}", "it.where.batch": "batch {batch}, {rows}",
        "it.p.sensors": "Sensors involved most: {list}.",
        "it.p.reviewed.accepted": "A person has accepted this.", "it.p.reviewed.questioned": "A person has questioned this.",
        "it.p.reviewed.overridden": "A person has corrected this.", "it.p.reviewed.dismissed": "A person has dismissed this.",
        "it.a.site.sensor": "Check {sensor} on site: wiring, calibration, and whether it is stuck",
        "it.a.site.sensor.nosig": "Check the sensors involved on site: wiring, calibration, stuck values",
        "it.a.site.process": "Ask the operators what changed around {where}: settings, feed, equipment",
        "it.a.site.data": "Check how these rows were recorded or exported",
        "it.a.site.mixed": "Check {sensor} first, then ask the operators what changed",
        "it.a.site.unknown": "Compare {where} with the operators' notes for that time",
        "it.a.site.points": "Compare these rows with the operators' notes and check the sensors that show up most often",
        "it.a.decide": "Accept, question or correct this finding",
        "it.a.open.diagnosis": "Open this finding", "it.a.open.flag": "See it on the timeline", "it.a.open.check": "See this check in its batch",
        "it.a.open.batch": "Open the batch and its checks", "it.a.open.generic": "Open it in its page",
        "it.a.rows": "See these rows on the timeline",
        "it.ask.diagnosis": "What should I check on site for {id}?",
        "it.ask.flag": "Why was {id} raised, and what should I check?",
        "it.ask.check": "Why is {id} a problem, and can I still use these rows?",
        "it.ask.batch": "Can I trust the data of batch {id}?",
        "it.ask.generic": "Explain {id} in simple words.",
        "it.fl.h.point": "A single reading does not fit its surroundings: a glitch or a manipulation; the data alone can't tell.",
        "it.fl.h.anomaly": "Unusual behaviour was found here, mainly on {sensor}.", "it.fl.h.anomaly.nosig": "Unusual behaviour was found here.",
        "it.fl.h.drift": "The readings slowly moved away from normal operation here, mainly on {sensor}.", "it.fl.h.drift.nosig": "The readings slowly moved away from normal operation here.",
        "it.fl.h.changepoint": "The behaviour changed suddenly here; {sensor} reacted first.", "it.fl.h.changepoint.nosig": "The behaviour changed suddenly here.",
        "it.fl.h.cascade": "A disturbance spread from sensor to sensor here, starting at {sensor}.", "it.fl.h.cascade.nosig": "A disturbance spread from sensor to sensor here.",
        "it.fl.h.dq": "This alarm is about the data, not the process: {problem} on {sensor}.", "it.fl.h.dq.nosig": "This alarm is about the data, not the process.",
        "it.fl.h.rule": "One of your operating rules was broken here.",
        "it.fl.p.cause.process": "Most likely cause: a real change in the process ({sure}).", "it.fl.p.cause.sensor": "Most likely cause: a faulty sensor ({sure}).",
        "it.fl.p.cause.data": "Most likely cause: a problem in the data ({sure}).", "it.fl.p.cause.mixed": "Most likely more than one cause is involved ({sure}).",
        "it.fl.p.cause.unknown": "The cause could not be determined.",
        "it.fl.p.strength": "It is {x} times stronger than anything seen in normal operation.",
        "it.ck.h.pass": "This check found nothing wrong.",
        "it.ck.h": "Data problem: {problem} on {sensor}.", "it.ck.h.nosig": "Data problem: {problem}.",
        "it.ck.p.use.pass": "These rows can be used.",
        "it.ck.p.use.warn": "The rows can still be used, with some care.",
        "it.ck.p.use.fail.sig": "These rows of this sensor are set aside. The other sensors can still be used.",
        "it.ck.p.use.fail.batch": "The whole batch is not trusted, so treat findings from it with caution.",
        "it.ck.p.use.fail": "The affected rows are set aside, so they are not mistaken for a fault in the process.",
        "it.ck.a.sensor": "Check {sensor} on site",
        "it.b.h.ok": "The data of batch {id} can be trusted.", "it.b.h.caution": "The data of batch {id} can be used, with smaller problems.", "it.b.h.bad": "The data of batch {id} cannot be trusted.",
        "it.b.p.problems": "Problems found: {list}.", "it.b.p.sensors": "Sensors affected: {list}.", "it.b.p.score": "It is rated {pct} trustworthy.",
        "it.b.p.none": "All checks passed.",
        "it.ev.h": "This is one measured fact that the explanations rely on.",
        "it.unknown.h": "{id} was not found in this run.",
        # problem words
        "pw.stuck": "frozen sensor values", "pw.stale": "sensors that stopped updating", "pw.missing": "missing values", "pw.dropout": "sensors that went silent",
        "pw.out_of_range": "values far outside the usual range", "pw.impossible_value": "impossible values", "pw.unit_shift": "sudden changes of scale (wrong unit or decimal point)",
        "pw.saturation": "sensors stuck at their limit", "pw.quantization_change": "changes in how finely values are recorded", "pw.sign_violation": "values with the wrong sign",
        "pw.relation_break": "sensors that stopped moving with their partners", "pw.local_spike": "single odd readings", "pw.out_of_order": "rows out of time order",
        "pw.duplicate": "duplicate rows", "pw.gap": "gaps in time", "pw.irregular_sampling": "uneven time between readings", "pw.empty_rows": "empty rows",
        "pw.redundancy_violation": "copies that no longer match their source", "pw.rule": "broken operating rules", "pw.other": "other data problems",
        "dq.h.caution.1": "The data can be used. One of {n} batches has smaller problems, mostly {top}.",
        "dq.h.bad.1": "One of {n} batches cannot be trusted. The main reason: {top}.",
        "ov.p.data.caution.1": "Data: one of {n} batches has smaller problems, mostly {top}.",
        "ov.p.data.bad.1": "Data: one of {n} batches cannot be trusted, mainly because of {top}.",
        "mon.cause.process.1": "1 looks like a real change in the process", "mon.cause.sensor.1": "1 looks like a faulty sensor",
        "mon.cause.data.1": "1 looks like a data problem", "mon.cause.mixed.1": "1 has more than one cause", "mon.cause.unknown.1": "1 could not be explained",
        "mon.p.points.1": "One single odd reading was also found. It is a glitch or a manipulation; the data alone can't tell.",
        "mon.p.dq.1": "One further alarm is about a problem in the data, not in the process.",
        "as.p.thin.1": "One of {n} operating conditions is rarely seen in the data, so faults there are harder to spot.",
        "as.p.recs.1": "The system has one suggestion for improving the data. Nothing is changed until you approve it.",
        "df.p.local.1": "The AI helper that words the explanations ran on this computer once.",
        "df.h.sent.1": "One short summary was sent to an outside AI service. A copy of your measurements never leaves this computer.",
        "df.h.blocked.1": "Your data stayed on this computer. One attempt to contact an outside AI service was stopped.",
        "lg.p.human.1": "One entry is a decision made by a person (accept, question, correct).",
        "lg.p.open.1": "One finding still waits for a person's decision.",
    },
    "fi": {
        "pending.h": "Tämä vaihe ei ole vielä valmis.",
        "pending.p": "Yhteenveto näkyy tässä heti, kun vaihe on valmis.",
        "pending.a": "Seuraa analyysin etenemistä",
        "failed.h": "Analyysi pysähtyi ennen kuin tämä vaihe valmistui.",
        "failed.p": "Kaikki sitä ennen valmistunut on edelleen nähtävissä muilla sivuilla.",
        "failed.a": "Katso, mikä meni vikaan",
        "skipped.h": "Tämä vaihe ohitettiin tässä ajossa, joten tässä ei ole näytettävää.",
        "sure.very": "hyvin varma", "sure.fairly": "melko varma", "sure.notvery": "ei kovin varma", "sure.unsure": "epävarma",
        "sureS.very": "järjestelmä on hyvin varma", "sureS.fairly": "järjestelmä on melko varma", "sureS.notvery": "järjestelmä ei ole kovin varma", "sureS.unsure": "järjestelmä on epävarma",
        "unit.s": "{n} sekunnin", "unit.s.1": "sekunnin", "unit.min": "{n} minuutin", "unit.min.1": "minuutin", "unit.h": "{n} tunnin", "unit.h.1": "tunnin",
        "sensor": "anturi {name}", "and": "ja", "rows": "rivit {a}–{b}", "rows.1": "rivi {a}",
        "und.h.ok": "Tiedosto luettiin ongelmitta: {rows} riviä {n} anturista.",
        "und.h.groups": "Tiedosto luettiin ongelmitta: {rows} riviä {n} anturista, jaettuna {g} ryhmään.",
        "und.h.odd": "Tiedosto luettiin, mutta se ei näytä tavalliselta anturidatalta. Tarkista, miten se luettiin.",
        "und.p.period": "Lukemia on noin {period} välein.",
        "und.p.noperiod": "Tiedostossa ei ole aikasaraketta, joten aika lasketaan riveinä.",
        "und.p.excluded": "{k} anturia {n}:stä jätetään pois vianetsinnästä, koska ne eivät muutu tai vain toistavat toista anturia.",
        "und.p.excluded.1": "Yksi anturi {n}:stä jätetään pois vianetsinnästä, koska se ei muutu tai vain toistaa toista anturia.",
        "und.p.names": "Järjestelmä ei tiedä, mitä kukin anturi mittaa. Se katsoo vain, miten arvot käyttäytyvät.",
        "und.p.unsure": "Järjestelmä joutui arvaamaan {k} asiaa tiedostosta. Kysy alta, mitä se oletti.",
        "und.p.unsure.1": "Järjestelmä joutui arvaamaan yhden asian tiedostosta. Kysy alta, mitä se oletti.",
        "und.a.check": "Tarkista, että sarakkeet luettiin odottamallasi tavalla",
        "und.a.name": "Kerro järjestelmälle, mitä anturisi mittaavat",
        "und.ask": "Mitä oletit datastani ja mistä olet epävarma?",
        "dq.h.ok": "Data on kunnossa: kaikki {n} erää läpäisivät tarkistukset.",
        "dq.h.caution": "Dataa voi käyttää. {k} erässä {n}:stä on pienempiä ongelmia, useimmiten: {top}.",
        "dq.h.bad": "{k} erään {n}:stä ei voi luottaa. Tärkein syy: {top}.",
        "dq.p.top": "Yleisimmät ongelmat: {list}.",
        "dq.p.sensors": "Eniten ongelmia näissä antureissa: {list}.",
        "dq.p.setaside": "Rivit, joissa on dataongelmia, siirretään sivuun, jotta niitä ei tulkita prosessin viaksi.",
        "dq.p.batch": "Erä on joukko rivejä, jotka kuuluvat ajallisesti yhteen.",
        "dq.a.worst": "Katso huonointa erää ({id})",
        "dq.a.check": "Katso vakavin dataongelma",
        "dq.a.checks": "Katso kaikki tarkistukset",
        "dq.ask": "Mitkä dataongelmat ovat tärkeimpiä, ja voinko silti käyttää tätä dataa?",
        "mon.h.none": "Mitään poikkeavaa ei löytynyt: prosessi toimi normaalisti koko ajan.",
        "mon.h.points": "{n} yksittäistä lukemaa ei sovi ympäristöönsä. Pidempään kestänyttä muutosta prosessissa ei löytynyt.",
        "mon.h.points.1": "Yksi yksittäinen lukema ei sovi ympäristöönsä. Pidempään kestänyttä muutosta prosessissa ei löytynyt.",
        "mon.h.events": "Poikkeavaa käyttäytymistä löytyi {k} kohdasta. Voimakkain: {where}, pääasiassa {sensor}.",
        "mon.h.events.short": "Poikkeavaa käyttäytymistä löytyi {k} kohdasta. Voimakkain: {where}.",
        "mon.h.events.1": "Poikkeavaa käyttäytymistä löytyi yhdestä kohdasta: {where}, pääasiassa {sensor}.",
        "mon.h.events.short.1": "Poikkeavaa käyttäytymistä löytyi yhdestä kohdasta: {where}.",
        "mon.p.causes": "Näistä {list}.",
        "mon.cause.process": "{n} näyttää todelliselta muutokselta prosessissa", "mon.cause.sensor": "{n} näyttää vialliselta anturilta",
        "mon.cause.data": "{n} näyttää dataongelmalta", "mon.cause.mixed": "{n}:ssä on useampi syy", "mon.cause.unknown": "{n} jäi selittämättä",
        "mon.p.points": "Lisäksi löytyi {n} yksittäistä outoa lukemaa. Jokainen on häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä.",
        "mon.p.points.only": "Jokainen niistä on häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä.",
        "mon.p.groups": "Poikkeamia oli {k} ryhmässä {g}:stä.",
        "mon.p.dq": "Lisäksi {n} hälytystä koskee datan ongelmia, ei prosessia.",
        "mon.p.learned": "Järjestelmä oppi normaalin toiminnan suoraan datastasi. Kenenkään ei tarvinnut merkitä vikoja.",
        "mon.a.top": "Katso voimakkainta tapahtumaa ({where})",
        "mon.a.sus": "Käy läpi epäilyttävien rivien luettelo",
        "mon.a.diag": "Lue, mikä sen todennäköisesti aiheutti",
        "mon.a.chart": "Katso koko ajon aikajana",
        "mon.ask": "Mitä tapahtui kohdassa {where}, ja mitä minun pitäisi tarkistaa?",
        "mon.ask.none": "Mistä tiedät, että prosessi toimi normaalisti?",
        "mon.ask.points": "Ovatko yksittäiset oudot lukemat anturiongelma vai jotain pahempaa?",
        "dg.h.none": "Vikaselitystä ei tarvittu: mitään riittävän poikkeavaa ei löytynyt.",
        "dg.h.top": "Tärkein löydös: {what} {where}. {sure}.",
        "dg.h.top.short": "Tärkein löydös: {what} {where}.",
        "dg.h.top.min": "Tärkein löydös: {what}.",
        "dg.what.process": "itse prosessi on todennäköisesti muuttunut", "dg.what.sensor": "{sensor} on todennäköisesti viallinen", "dg.what.sensor.nosig": "jokin anturi on todennäköisesti viallinen",
        "dg.what.data": "ongelma datassa, ei prosessissa,", "dg.what.mixed": "sekä prosessi että anturi näyttävät olevan osallisina",
        "dg.what.unknown": "jotain poikkeavaa, jota järjestelmä ei osaa selittää", "dg.what.points": "{n} yksittäistä outoa lukemaa (häiriöitä tai manipulointia)",
        "dg.where.rows": "kohdassa {rows}", "dg.where.group": "ryhmässä {g}", "dg.where.both": "ryhmässä {g}, {rows}", "dg.where.none": "",
        "dg.p.count": "Löydöksiä on yhteensä {n}: {list}.",
        "dg.one.process": "Löydöksiä on yksi, ja se koskee prosessia.", "dg.one.sensor": "Löydöksiä on yksi, ja se koskee viallista anturia.", "dg.one.data": "Löydöksiä on yksi, ja se koskee dataongelmaa.",
        "dg.one.mixed": "Löydöksiä on yksi, ja sillä on useampi syy.", "dg.one.unknown": "Löydöksiä on yksi, eikä sen syytä pystytty määrittämään.",
        "dg.count.process": "{n} koskee prosessia", "dg.count.sensor": "{n} koskee viallisia antureita", "dg.count.data": "{n} koskee dataongelmia",
        "dg.count.mixed": "{n}:ssä on useampi syy", "dg.count.unknown": "{n} jäi selittämättä",
        "dg.p.challenge": "Järjestelmä tarkisti jokaisen löydöksen uudelleen: {list}.", "dg.p.challenge.all": "Järjestelmä tarkisti jokaisen löydöksen uudelleen, ja kaikki {n} kestivät.", "dg.p.challenge.all.1": "Järjestelmä tarkisti löydöksen uudelleen, ja se kesti.",
        "dg.ch.supported": "{n} kesti", "dg.ch.weakened": "{n} on epävarmalla pohjalla", "dg.ch.rejected": "{n} ei kestänyt",
        "dg.p.review": "Ihminen on tähän mennessä käynyt läpi {k}/{n}.",
        "dg.p.review.none": "Kukaan ei ole vielä käynyt niitä läpi. Hyväksyntäsi, kysymyksesi tai korjauksesi kirjataan.",
        "dg.a.open": "Avaa tärkein löydös",
        "dg.a.decide": "Hyväksy, kyseenalaista tai korjaa se",
        "dg.ask": "Mitä minun pitäisi tarkistaa paikan päällä kohteelle {id}?",
        "as.h.good": "Datasi riittää luotettavaan vianetsintään (kokonaisarvio {pct}).",
        "as.h.fair": "Dataasi voi käyttää, mutta paremmalla datalla vianetsintä olisi luotettavampaa (kokonaisarvio {pct}).",
        "as.h.weak": "Datasi on liian heikkoa luotettavaan vianetsintään (kokonaisarvio {pct}).",
        "as.h.noscore": "Datan hyödyllisyyden tarkistus on valmis.",
        "as.p.more.yes": "Lisää samanlaista dataa todennäköisesti auttaisi.", "as.p.more.no": "Lisää samanlaista dataa tuskin auttaisi paljon.",
        "as.p.more.unclear": "On epäselvää, auttaisiko lisädata.",
        "as.p.less.yes": "Huonojen rivien poistaminen todennäköisesti auttaisi.", "as.p.less.no": "Rivien poistaminen tuskin auttaisi.",
        "as.p.less.unclear": "On epäselvää, auttaisiko rivien poistaminen.",
        "as.p.thin": "{k} käyttötilannetta {n}:stä näkyy datassa harvoin, joten viat niissä on vaikeampi huomata.",
        "as.p.recs": "Järjestelmällä on {n} ehdotusta datan parantamiseksi. Mitään ei muuteta ennen hyväksyntääsi.",
        "as.a.recs": "Katso ehdotukset datan parantamiseksi",
        "as.ask": "Auttaisiko lisädata, ja millainen?",
        "as.ask2": "Mitkä rivit tai anturit kannattaisi poistaa, jotta tulokset paranisivat?",
        "df.h.local": "Datasi pysyi tällä tietokoneella. Mitään ei lähetetty internetiin.",
        "df.h.sent": "{n} lyhyttä yhteenvetoa lähetettiin ulkopuoliselle tekoälypalvelulle. Mittaustesi kopio ei koskaan poistu tältä tietokoneelta.",
        "df.h.blocked": "Datasi pysyi tällä tietokoneella. {b} yritystä ottaa yhteys ulkopuoliseen tekoälypalveluun estettiin.",
        "df.p.local": "Selitykset muotoileva tekoälyapuri toimi tällä tietokoneella {n} kertaa.",
        "df.p.nolocal": "Tekoälyapuria ei käytetty. Kaikki tekstit tulevat ohjelman kiinteistä säännöistä.",
        "df.p.mode.off": "Nykyinen asetus: ulkopuoliset palvelut on kytketty pois.",
        "df.p.mode.on": "Nykyinen asetus: ulkopuolinen palvelu voi saada lyhyitä yhteenvetoja, ei koskaan datasi rivejä.",
        "df.p.list": "Jokainen tekoälyapurin käyttökerta kirjataan, ja kirjaukset voi tarkastaa.",
        "df.a.list": "Katso luettelo siitä, mitä lähetettiin ja minne",
        "df.a.mode": "Tarkista tai muuta yksityisyysasetusta",
        "df.ask": "Poistuiko mitään datastani tältä tietokoneelta?",
        "lg.h.ok": "Kaikki järjestelmän ja ihmisten toimet on kirjattu: {n} merkintää, eikä kirjanpitoa ole muutettu.",
        "lg.h.plain": "Kaikki järjestelmän ja ihmisten toimet on kirjattu: {n} merkintää.",
        "lg.h.bad": "Varoitus: päätösten kirjanpitoa on muutettu jälkikäteen, ensimmäisen kerran merkinnässä {seq}.",
        "lg.p.human": "{k} merkintää on ihmisten tekemiä päätöksiä (hyväksy, kyseenalaista, korjaa).",
        "lg.p.human.none": "Kukaan ei ole vielä tehnyt päätöstä tässä ajossa.",
        "lg.p.open": "{k} löydöstä odottaa vielä ihmisen päätöstä.",
        "lg.p.why": "Kirjanpitoa ei voi muokata huomaamatta, joten se todistaa, kuka päätti mitä.",
        "lg.a.verify": "Tarkista kirjanpito itse",
        "lg.a.review": "Käy läpi päätöstä odottavat löydökset",
        "lg.ask": "Kuka päätti mitä tässä ajossa?",
        "rp.h.ready": "Tämän ajon raportti on valmis luettavaksi, ladattavaksi tai lähetettäväksi.",
        "rp.h.can": "Tämän ajon raportin voi tehdä nyt. Se kestää muutaman sekunnin.",
        "rp.p.formats": "Sen saa englanniksi, suomeksi ja ruotsiksi: verkkosivuna, PDF:nä tai diaesityksenä.",
        "rp.p.content": "Se kertoo, kuinka hyvää data on, mitä poikkeavaa tapahtui, mitkä ovat todennäköiset syyt ja mikä jää epävarmaksi.",
        "rp.p.local": "Raportti tehdään tällä tietokoneella.",
        "rp.a.open": "Lue raportti",
        "rp.a.send": "Lähetä se sähköpostilla",
        "rp.ask": "Tiivistä tämä ajo viiteen virkkeeseen esihenkilölleni.",
        "ov.h.running": "Analyysi on vielä käynnissä: {done}/{total} vaihetta on valmiina.",
        "ov.h.failed": "Analyysi pysähtyi vaiheeseen \"{stage}\". Aiemmat tulokset ovat edelleen käytettävissä.",
        "ov.h": "{data}, ja {proc}.",
        "ov.data.ok": "Datasi on kunnossa", "ov.data.caution": "Datasi on käyttökelpoista", "ov.data.bad": "Osaan datastasi ei voi luottaa", "ov.data.pending": "Tiedostosi luettiin",
        "ov.proc.none": "prosessissa ei havaittu mitään poikkeavaa", "ov.proc.points": "{n} yksittäistä lukemaa näyttää oudolta", "ov.proc.points.1": "yksi yksittäinen lukema näyttää oudolta",
        "ov.proc.events": "poikkeavaa käyttäytymistä löytyi {k} kohdasta", "ov.proc.events.1": "poikkeavaa käyttäytymistä löytyi yhdestä kohdasta",
        "ov.proc.pending": "poikkeavan käyttäytymisen etsintä on vielä kesken",
        "ov.p.data.ok": "Data: kaikki {n} erää läpäisivät tarkistukset.",
        "ov.p.data.caution": "Data: {k} erässä {n}:stä on pienempiä ongelmia, useimmiten: {top}.",
        "ov.p.data.bad": "Data: {k} erään {n}:stä ei voi luottaa. Tärkein syy: {top}.",
        "ov.p.first": "Katso ensin tätä: {what} {where}.",
        "ov.p.first.min": "Katso ensin tätä: {what}.",
        "ov.a.diag": "Avaa tärkein löydös",
        "ov.a.batch": "Katso huonointa erää ({id})",
        "ov.a.sus": "Käy läpi epäilyttävät rivit",
        "ov.a.report": "Lue tai lähetä raportti",
        "ov.a.progress": "Seuraa analyysin etenemistä",
        "ov.ask": "Mitä minun pitäisi tehdä ensin?",
        "stage.ingest": "tiedoston luku", "stage.profile": "antureiden ymmärtäminen", "stage.quality": "datan tarkistus", "stage.detect": "poikkeavan käyttäytymisen etsintä",
        "stage.diagnose": "löydösten selittäminen", "stage.assess": "datan arviointi", "stage.report": "raportin kirjoitus",
        "it.dg.h.process": "Itse prosessi on todennäköisesti muuttunut tässä.",
        "it.dg.h.sensor": "{sensor} on todennäköisesti viallinen.", "it.dg.h.sensor.nosig": "Jokin anturi on todennäköisesti viallinen.",
        "it.dg.h.data": "Tämä on todennäköisesti ongelma datassa, ei prosessissa.",
        "it.dg.h.data.sig": "{sensor}: lukemat ovat tässä todennäköisesti virheellisiä; itse prosessi näyttää olevan kunnossa.",
        "it.dg.h.mixed": "Sekä prosessi että anturi näyttävät olevan tässä osallisina.",
        "it.dg.h.unknown": "Tässä tapahtui jotain poikkeavaa, mutta järjestelmä ei osaa sanoa, johtuiko se prosessista vai anturista.",
        "it.dg.h.points": "{n} yksittäistä lukemaa ei sovi ympäristöönsä: häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä.",
        "it.p.sure": "Kuinka varmaa: {sure} ({pct}).",
        "it.p.sure.weakened": "Kuinka varmaa: {sure} ({pct}), ja sen oma uusintatarkistus löysi heikkoja kohtia.",
        "it.p.sure.rejected": "Kuinka varmaa: {sure} ({pct}). Järjestelmän oma uusintatarkistus ei tukenut tätä, joten älä luota siihen.",
        "it.p.where": "Missä: {where}.", "it.p.where.time": "Missä: {where}, {t0} – {t1}.",
        "it.p.where.points": "Missä: {n} kohtaa eri puolilla dataa; epäilyttävien rivien luettelo näyttää jokaisen.",
        "it.where.rows": "{rows}", "it.where.group": "ryhmä {g}, {rows}", "it.where.batch": "erä {batch}, {rows}",
        "it.p.sensors": "Eniten osallisina olevat anturit: {list}.",
        "it.p.reviewed.accepted": "Ihminen on hyväksynyt tämän.", "it.p.reviewed.questioned": "Ihminen on kyseenalaistanut tämän.",
        "it.p.reviewed.overridden": "Ihminen on korjannut tätä.", "it.p.reviewed.dismissed": "Ihminen on hylännyt tämän.",
        "it.a.site.sensor": "Tarkista paikan päällä {sensor}: johdotus, kalibrointi ja onko arvo jumissa",
        "it.a.site.sensor.nosig": "Tarkista osalliset anturit paikan päällä: johdotus, kalibrointi, jumiutuneet arvot",
        "it.a.site.process": "Kysy operaattoreilta, mikä muuttui kohdassa {where}: asetukset, syöttö, laitteet",
        "it.a.site.data": "Tarkista, miten nämä rivit tallennettiin tai vietiin",
        "it.a.site.mixed": "Tarkista ensin {sensor} ja kysy sitten operaattoreilta, mikä muuttui",
        "it.a.site.unknown": "Vertaa kohtaa {where} operaattoreiden muistiinpanoihin samalta ajalta",
        "it.a.site.points": "Vertaa näitä rivejä operaattoreiden muistiinpanoihin ja tarkista useimmin esiintyvät anturit",
        "it.a.decide": "Hyväksy, kyseenalaista tai korjaa tämä löydös",
        "it.a.open.diagnosis": "Avaa tämä löydös", "it.a.open.flag": "Katso se aikajanalla", "it.a.open.check": "Katso tämä tarkistus erässään",
        "it.a.open.batch": "Avaa erä ja sen tarkistukset", "it.a.open.generic": "Avaa se omalla sivullaan",
        "it.a.rows": "Katso nämä rivit aikajanalla",
        "it.ask.diagnosis": "Mitä minun pitäisi tarkistaa paikan päällä kohteelle {id}?",
        "it.ask.flag": "Miksi {id} nostettiin esiin, ja mitä minun pitäisi tarkistaa?",
        "it.ask.check": "Miksi {id} on ongelma, ja voinko silti käyttää näitä rivejä?",
        "it.ask.batch": "Voinko luottaa erän {id} dataan?",
        "it.ask.generic": "Selitä {id} yksinkertaisin sanoin.",
        "it.fl.h.point": "Yksittäinen lukema ei sovi ympäristöönsä: häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä.",
        "it.fl.h.anomaly": "Tässä havaittiin poikkeavaa käyttäytymistä, pääasiassa {sensor}.", "it.fl.h.anomaly.nosig": "Tässä havaittiin poikkeavaa käyttäytymistä.",
        "it.fl.h.drift": "Prosessi liukui tässä hitaasti pois normaalista toiminnasta, pääasiassa {sensor}.", "it.fl.h.drift.nosig": "Prosessi liukui tässä hitaasti pois normaalista toiminnasta.",
        "it.fl.h.changepoint": "Prosessin käyttäytyminen muuttui tässä äkillisesti; ensimmäisenä reagoi {sensor}.", "it.fl.h.changepoint.nosig": "Prosessin käyttäytyminen muuttui tässä äkillisesti.",
        "it.fl.h.cascade": "Häiriö levisi tässä anturilta toiselle; se alkoi kohteesta {sensor}.", "it.fl.h.cascade.nosig": "Häiriö levisi tässä anturilta toiselle.",
        "it.fl.h.dq": "Tämä hälytys koskee dataa, ei prosessia: {problem}, {sensor}.", "it.fl.h.dq.nosig": "Tämä hälytys koskee dataa, ei prosessia.",
        "it.fl.h.rule": "Yhtä käyttösäännöistäsi rikottiin tässä.",
        "it.fl.p.cause.process": "Todennäköisin syy: todellinen muutos prosessissa ({sure}).", "it.fl.p.cause.sensor": "Todennäköisin syy: viallinen anturi ({sure}).",
        "it.fl.p.cause.data": "Todennäköisin syy: ongelma datassa ({sure}).", "it.fl.p.cause.mixed": "Todennäköisesti taustalla on useampi syy ({sure}).",
        "it.fl.p.cause.unknown": "Syytä ei pystytty määrittämään.",
        "it.fl.p.strength": "Se on {x} kertaa voimakkaampi kuin mikään normaalissa toiminnassa nähty.",
        "it.ck.h.pass": "Tämä tarkistus ei löytänyt mitään vikaa.",
        "it.ck.h": "Dataongelma: {problem}, {sensor}.", "it.ck.h.nosig": "Dataongelma: {problem}.",
        "it.ck.p.use.pass": "Näitä rivejä voi käyttää.",
        "it.ck.p.use.warn": "Rivejä voi silti käyttää, kunhan on varovainen.",
        "it.ck.p.use.fail.sig": "Tämän anturin nämä rivit siirretään sivuun. Muita antureita voi silti käyttää.",
        "it.ck.p.use.fail.batch": "Koko erään ei luoteta, joten suhtaudu sen löydöksiin varoen.",
        "it.ck.p.use.fail": "Ongelmarivit siirretään sivuun, jotta niitä ei tulkita prosessin viaksi.",
        "it.ck.a.sensor": "Tarkista paikan päällä {sensor}",
        "it.b.h.ok": "Erän {id} dataan voi luottaa.", "it.b.h.caution": "Erän {id} dataa voi käyttää, siinä on pienempiä ongelmia.", "it.b.h.bad": "Erän {id} dataan ei voi luottaa.",
        "it.b.p.problems": "Löydetyt ongelmat: {list}.", "it.b.p.sensors": "Anturit, joita ongelmat koskevat: {list}.", "it.b.p.score": "Sen luotettavuusarvio on {pct}.",
        "it.b.p.none": "Kaikki tarkistukset menivät läpi.",
        "it.ev.h": "Tämä on yksi mitattu tosiasia, johon selitykset nojaavat.",
        "it.unknown.h": "Kohdetta {id} ei löytynyt tästä ajosta.",
        "pw.stuck": "jumiutuneet anturiarvot", "pw.stale": "päivittymättömät anturit", "pw.missing": "puuttuvat arvot", "pw.dropout": "hiljenneet anturit",
        "pw.out_of_range": "arvot kaukana tavallisesta alueesta", "pw.impossible_value": "mahdottomat arvot", "pw.unit_shift": "äkilliset mittakaavan muutokset (väärä yksikkö tai desimaalipilkku)",
        "pw.saturation": "rajaansa juuttuneet anturit", "pw.quantization_change": "muutokset arvojen tallennustarkkuudessa", "pw.sign_violation": "väärän etumerkin arvot",
        "pw.relation_break": "anturit, jotka eivät enää seuraa pariaan", "pw.local_spike": "yksittäiset oudot lukemat", "pw.out_of_order": "rivit väärässä aikajärjestyksessä",
        "pw.duplicate": "kaksoisrivit", "pw.gap": "aukot ajassa", "pw.irregular_sampling": "epätasainen lukemien väli", "pw.empty_rows": "tyhjät rivit",
        "pw.redundancy_violation": "kopiot, jotka eivät enää vastaa lähdettään", "pw.rule": "rikotut käyttösäännöt", "pw.other": "muut dataongelmat",
        "dq.h.bad.1": "Yhteen erään {n}:stä ei voi luottaa. Tärkein syy: {top}.",
        "ov.p.data.bad.1": "Data: yhteen erään {n}:stä ei voi luottaa. Tärkein syy: {top}.",
        "mon.p.points.1": "Lisäksi löytyi yksi yksittäinen outo lukema. Se on häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä.",
        "mon.p.dq.1": "Lisäksi yksi hälytys koskee datan ongelmaa, ei prosessia.",
        "as.p.thin.1": "Yksi käyttötilanne {n}:stä näkyy datassa harvoin, joten viat siinä on vaikeampi huomata.",
        "as.p.recs.1": "Järjestelmällä on yksi ehdotus datan parantamiseksi. Mitään ei muuteta ennen hyväksyntääsi.",
        "df.p.local.1": "Selitykset muotoileva tekoälyapuri toimi tällä tietokoneella yhden kerran.",
        "df.h.sent.1": "Yksi lyhyt yhteenveto lähetettiin ulkopuoliselle tekoälypalvelulle. Mittaustesi kopio ei koskaan poistu tältä tietokoneelta.",
        "df.h.blocked.1": "Datasi pysyi tällä tietokoneella. Yksi yritys ottaa yhteys ulkopuoliseen tekoälypalveluun estettiin.",
        "lg.p.human.1": "Yksi merkintä on ihmisen tekemä päätös (hyväksy, kyseenalaista, korjaa).",
        "lg.p.open.1": "Yksi löydös odottaa vielä ihmisen päätöstä.",
    },
    "sv": {
        "pending.h": "Det här steget är inte klart ännu.",
        "pending.p": "Sammanfattningen visas här så snart steget är klart.",
        "pending.a": "Följ hur analysen går",
        "failed.h": "Analysen stannade innan det här steget blev klart.",
        "failed.p": "Allt som blev klart före det finns kvar på de andra sidorna.",
        "failed.a": "Se vad som gick fel",
        "skipped.h": "Det här steget hoppades över i den här körningen, så här finns inget att visa.",
        "sure.very": "mycket säkert", "sure.fairly": "ganska säkert", "sure.notvery": "inte särskilt säkert", "sure.unsure": "osäkert",
        "sureS.very": "systemet är mycket säkert", "sureS.fairly": "systemet är ganska säkert", "sureS.notvery": "systemet är inte särskilt säkert", "sureS.unsure": "systemet är osäkert",
        "unit.s": "{n} sekunders", "unit.s.1": "en sekunds", "unit.min": "{n} minuters", "unit.min.1": "en minuts", "unit.h": "{n} timmars", "unit.h.1": "en timmes",
        "sensor": "givare {name}", "and": "och", "rows": "rad {a}–{b}", "rows.1": "rad {a}",
        "und.h.ok": "Filen lästes utan problem: {rows} rader från {n} givare.",
        "und.h.groups": "Filen lästes utan problem: {rows} rader från {n} givare, uppdelade i {g} grupper.",
        "und.h.odd": "Filen lästes, men den ser inte ut som vanliga givardata. Kontrollera hur den lästes.",
        "und.p.period": "Mätvärdena kommer med ungefär {period} mellanrum.",
        "und.p.noperiod": "Filen har ingen tidskolumn, så tiden räknas i rader.",
        "und.p.excluded": "{k} av {n} givare lämnas utanför felsökningen eftersom de aldrig ändras eller bara upprepar en annan givare.",
        "und.p.excluded.1": "En av {n} givare lämnas utanför felsökningen eftersom den aldrig ändras eller bara upprepar en annan givare.",
        "und.p.names": "Systemet vet inte vad varje givare mäter. Det ser bara på hur värdena beter sig.",
        "und.p.unsure": "Systemet fick gissa {k} saker om filen. Fråga nedan vad det antog.",
        "und.p.unsure.1": "Systemet fick gissa en sak om filen. Fråga nedan vad det antog.",
        "und.a.check": "Kontrollera att kolumnerna lästes som du väntar dig",
        "und.a.name": "Berätta för systemet vad dina givare mäter",
        "und.ask": "Vad antog du om mina data, och vad är du osäker på?",
        "dq.h.ok": "Data är i ordning: alla {n} batchar klarade kontrollerna.",
        "dq.h.caution": "Data går att använda. {k} av {n} batchar har mindre problem, oftast {top}.",
        "dq.h.bad": "{k} av {n} batchar går inte att lita på. Främsta orsaken: {top}.",
        "dq.p.top": "Vanligaste problemen: {list}.",
        "dq.p.sensors": "Givare med flest problem: {list}.",
        "dq.p.setaside": "Rader med dataproblem läggs åt sidan, så att de inte misstas för ett fel i processen.",
        "dq.p.batch": "En batch är ett block av rader som hör ihop i tid.",
        "dq.a.worst": "Titta på den sämsta batchen ({id})",
        "dq.a.check": "Se det allvarligaste dataproblemet",
        "dq.a.checks": "Se alla kontroller",
        "dq.ask": "Vilka dataproblem är viktigast, och kan jag ändå använda dessa data?",
        "mon.h.none": "Inget ovanligt hittades: processen betedde sig normalt hela tiden.",
        "mon.h.points": "{n} enstaka mätvärden passar inte in i sin omgivning. Ingen mer långvarig förändring i processen hittades.",
        "mon.h.points.1": "Ett enstaka mätvärde passar inte in i sin omgivning. Ingen mer långvarig förändring i processen hittades.",
        "mon.h.events": "Ovanligt beteende hittades på {k} ställen. Det starkaste: {where}, främst {sensor}.",
        "mon.h.events.short": "Ovanligt beteende hittades på {k} ställen. Det starkaste: {where}.",
        "mon.h.events.1": "Ovanligt beteende hittades på ett ställe: {where}, främst {sensor}.",
        "mon.h.events.short.1": "Ovanligt beteende hittades på ett ställe: {where}.",
        "mon.p.causes": "Av dessa: {list}.",
        "mon.cause.process": "{n} ser ut som en verklig förändring i processen", "mon.cause.sensor": "{n} ser ut som en trasig givare",
        "mon.cause.data": "{n} ser ut som ett dataproblem", "mon.cause.mixed": "{n} har mer än en orsak", "mon.cause.unknown": "{n} gick inte att förklara",
        "mon.p.points": "Dessutom hittades {n} enstaka udda mätvärden. Vart och ett är en störning eller en manipulation; enbart data kan inte avgöra det.",
        "mon.p.points.only": "Vart och ett är en störning eller en manipulation; enbart data kan inte avgöra det.",
        "mon.p.groups": "{k} av {g} grupper berördes.",
        "mon.p.dq": "Ytterligare {n} larm gäller problem i data, inte i processen.",
        "mon.p.learned": "Systemet lärde sig hur normal drift ser ut direkt ur dina data. Ingen behövde markera felen.",
        "mon.a.top": "Titta på den starkaste händelsen ({where})",
        "mon.a.sus": "Gå igenom listan över misstänkta rader",
        "mon.a.diag": "Läs vad som troligen orsakade den",
        "mon.a.chart": "Se tidslinjen för hela körningen",
        "mon.ask": "Vad hände vid {where}, och vad bör jag kontrollera?",
        "mon.ask.none": "Hur vet du att processen var normal?",
        "mon.ask.points": "Är de enstaka udda mätvärdena ett givarproblem eller något värre?",
        "dg.h.none": "Ingen felförklaring behövdes: inget tillräckligt ovanligt hittades.",
        "dg.h.top": "Viktigaste fyndet: {what} {where}. {sure}.",
        "dg.h.top.short": "Viktigaste fyndet: {what} {where}.",
        "dg.h.top.min": "Viktigaste fyndet: {what}.",
        "dg.what.process": "själva processen har troligen ändrats", "dg.what.sensor": "{sensor} är troligen trasig", "dg.what.sensor.nosig": "en givare är troligen trasig",
        "dg.what.data": "ett problem i data, inte i processen,", "dg.what.mixed": "både processen och en givare verkar vara inblandade",
        "dg.what.unknown": "något ovanligt som systemet inte kan förklara", "dg.what.points": "{n} enstaka udda mätvärden (störningar eller manipulation)",
        "dg.where.rows": "vid {rows}", "dg.where.group": "i grupp {g}", "dg.where.both": "i grupp {g}, {rows}", "dg.where.none": "",
        "dg.p.count": "Totalt {n} fynd: {list}.",
        "dg.one.process": "Det finns ett fynd, och det gäller processen.", "dg.one.sensor": "Det finns ett fynd, och det gäller en trasig givare.", "dg.one.data": "Det finns ett fynd, och det gäller ett dataproblem.",
        "dg.one.mixed": "Det finns ett fynd, med mer än en orsak.", "dg.one.unknown": "Det finns ett fynd, och orsaken gick inte att fastställa.",
        "dg.count.process": "{n} om processen", "dg.count.sensor": "{n} om trasiga givare", "dg.count.data": "{n} om dataproblem",
        "dg.count.mixed": "{n} med mer än en orsak", "dg.count.unknown": "{n} oförklarade",
        "dg.p.challenge": "Systemet dubbelkollade varje fynd: {list}.", "dg.p.challenge.all": "Systemet dubbelkollade varje fynd, och alla {n} höll.", "dg.p.challenge.all.1": "Systemet dubbelkollade fyndet, och det höll.",
        "dg.ch.supported": "{n} höll", "dg.ch.weakened": "{n} står på svag grund", "dg.ch.rejected": "{n} höll inte",
        "dg.p.review": "{k} av {n} har hittills granskats av en människa.",
        "dg.p.review.none": "Ingen människa har granskat dem ännu. Ditt godkännande, din fråga eller din rättelse sparas.",
        "dg.a.open": "Öppna det viktigaste fyndet",
        "dg.a.decide": "Godkänn, ifrågasätt eller rätta det",
        "dg.ask": "Vad bör jag kontrollera på plats för {id}?",
        "as.h.good": "Dina data räcker för pålitlig felsökning (helhetsbetyg {pct}).",
        "as.h.fair": "Dina data går att använda, men felsökningen blir pålitligare med bättre data (helhetsbetyg {pct}).",
        "as.h.weak": "Dina data är för svaga för pålitlig felsökning (helhetsbetyg {pct}).",
        "as.h.noscore": "Kontrollen av hur användbara dina data är har blivit klar.",
        "as.p.more.yes": "Mer data av samma slag skulle troligen hjälpa.", "as.p.more.no": "Mer data av samma slag skulle troligen inte hjälpa särskilt mycket.",
        "as.p.more.unclear": "Det är oklart om mer data skulle hjälpa.",
        "as.p.less.yes": "Att ta bort de dåliga raderna skulle troligen hjälpa.", "as.p.less.no": "Att ta bort rader skulle troligen inte hjälpa.",
        "as.p.less.unclear": "Det är oklart om det skulle hjälpa att ta bort rader.",
        "as.p.thin": "{k} av {n} driftlägen syns sällan i data, så fel där är svårare att upptäcka.",
        "as.p.recs": "Systemet har {n} förslag för att förbättra data. Inget ändras förrän du godkänner det.",
        "as.a.recs": "Se förslagen för att förbättra data",
        "as.ask": "Skulle mer data hjälpa, och i så fall vilken sorts?",
        "as.ask2": "Vilka rader eller givare bör jag ta bort för att få bättre resultat?",
        "df.h.local": "Dina data stannade på den här datorn. Inget skickades till internet.",
        "df.h.sent": "{n} korta sammanfattningar skickades till en extern AI-tjänst. En kopia av dina mätvärden lämnar aldrig den här datorn.",
        "df.h.blocked": "Dina data stannade på den här datorn. {b} försök att kontakta en extern AI-tjänst stoppades.",
        "df.p.local": "AI-hjälpen som formulerar förklaringarna kördes på den här datorn {n} gånger.",
        "df.p.nolocal": "Ingen AI-hjälp användes. Alla texter kommer från fasta regler i programmet.",
        "df.p.mode.off": "Nuvarande inställning: externa tjänster är avstängda.",
        "df.p.mode.on": "Nuvarande inställning: en extern tjänst kan få korta sammanfattningar, aldrig rader ur dina data.",
        "df.p.list": "Varje användning av en AI-hjälp skrivs upp och kan granskas.",
        "df.a.list": "Se listan över vad som skickades vart",
        "df.a.mode": "Kontrollera eller ändra sekretessinställningen",
        "df.ask": "Har något av mina data lämnat den här datorn?",
        "lg.h.ok": "Allt som systemet och människor har gjort är nedskrivet: {n} poster, och ingen har ändrat dem.",
        "lg.h.plain": "Allt som systemet och människor har gjort är nedskrivet: {n} poster.",
        "lg.h.bad": "Varning: beslutsloggen har ändrats i efterhand, först vid post {seq}.",
        "lg.p.human": "{k} poster är beslut som människor har fattat (godkänn, ifrågasätt, rätta).",
        "lg.p.human.none": "Ingen människa har fattat något beslut i den här körningen ännu.",
        "lg.p.open": "{k} fynd väntar fortfarande på ett beslut av en människa.",
        "lg.p.why": "Loggen kan inte ändras utan att det syns, så den visar vem som beslutade vad.",
        "lg.a.verify": "Kontrollera loggen själv",
        "lg.a.review": "Gå igenom fynden som väntar på beslut",
        "lg.ask": "Vem beslutade vad i den här körningen?",
        "rp.h.ready": "Rapporten för den här körningen är klar att läsa, ladda ner eller skicka.",
        "rp.h.can": "En rapport för den här körningen kan skapas nu. Det tar några sekunder.",
        "rp.p.formats": "Den finns på engelska, finska och svenska, som webbsida, PDF eller bildspel.",
        "rp.p.content": "Den tar upp hur bra data är, vad ovanligt som hände, de troliga orsakerna och vad som fortfarande är osäkert.",
        "rp.p.local": "Rapporten skapas på den här datorn.",
        "rp.a.open": "Läs rapporten",
        "rp.a.send": "Skicka den med e-post",
        "rp.ask": "Sammanfatta den här körningen i fem meningar för min chef.",
        "ov.h.running": "Analysen pågår fortfarande: {done} av {total} steg är klara.",
        "ov.h.failed": "Analysen stannade vid steget \"{stage}\". Tidigare resultat finns kvar.",
        "ov.h": "{data}, och {proc}.",
        "ov.data.ok": "Dina data är i ordning", "ov.data.caution": "Dina data går att använda", "ov.data.bad": "Delar av dina data går inte att lita på", "ov.data.pending": "Din fil har lästs",
        "ov.proc.none": "inget ovanligt hittades i processen", "ov.proc.points": "{n} enstaka mätvärden ser udda ut", "ov.proc.points.1": "ett enstaka mätvärde ser udda ut",
        "ov.proc.events": "ovanligt beteende hittades på {k} ställen", "ov.proc.events.1": "ovanligt beteende hittades på ett ställe",
        "ov.proc.pending": "sökningen efter ovanligt beteende är inte klar ännu",
        "ov.p.data.ok": "Data: alla {n} batchar klarade kontrollerna.",
        "ov.p.data.caution": "Data: {k} av {n} batchar har mindre problem, oftast {top}.",
        "ov.p.data.bad": "Data: {k} av {n} batchar går inte att lita på, främst på grund av {top}.",
        "ov.p.first": "Titta först på detta: {what} {where}.",
        "ov.p.first.min": "Titta först på detta: {what}.",
        "ov.a.diag": "Öppna det viktigaste fyndet",
        "ov.a.batch": "Titta på den sämsta batchen ({id})",
        "ov.a.sus": "Gå igenom de misstänkta raderna",
        "ov.a.report": "Läs eller skicka rapporten",
        "ov.a.progress": "Följ hur analysen går",
        "ov.ask": "Vad bör jag göra först?",
        "stage.ingest": "läsa filen", "stage.profile": "förstå givarna", "stage.quality": "kontrollera data", "stage.detect": "leta efter ovanligt beteende",
        "stage.diagnose": "förklara fynden", "stage.assess": "betygsätta data", "stage.report": "skriva rapporten",
        "it.dg.h.process": "Själva processen har troligen ändrats här.",
        "it.dg.h.sensor": "{sensor} är troligen trasig.", "it.dg.h.sensor.nosig": "En givare är troligen trasig.",
        "it.dg.h.data": "Det här är troligen ett problem i data, inte i processen.",
        "it.dg.h.data.sig": "Mätvärdena från {sensor} är troligen felaktiga här; själva processen ser bra ut.",
        "it.dg.h.mixed": "Både processen och en givare verkar vara inblandade här.",
        "it.dg.h.unknown": "Något ovanligt hände här, men systemet kan inte avgöra om processen eller en givare orsakade det.",
        "it.dg.h.points": "{n} enstaka mätvärden passar inte in i sin omgivning: en störning eller en manipulation; enbart data kan inte avgöra det.",
        "it.p.sure": "Hur säkert: {sure} ({pct}).",
        "it.p.sure.weakened": "Hur säkert: {sure} ({pct}), och dess egen dubbelkoll hittade svaga punkter.",
        "it.p.sure.rejected": "Hur säkert: {sure} ({pct}). Systemets egen dubbelkoll stödde inte detta, så lita inte på det.",
        "it.p.where": "Var: {where}.", "it.p.where.time": "Var: {where}, från {t0} till {t1}.",
        "it.p.where.points": "Var: {n} ställen utspridda över data; listan över misstänkta rader visar vart och ett.",
        "it.where.rows": "{rows}", "it.where.group": "grupp {g}, {rows}", "it.where.batch": "batch {batch}, {rows}",
        "it.p.sensors": "Mest inblandade givare: {list}.",
        "it.p.reviewed.accepted": "En människa har godkänt detta.", "it.p.reviewed.questioned": "En människa har ifrågasatt detta.",
        "it.p.reviewed.overridden": "En människa har rättat detta.", "it.p.reviewed.dismissed": "En människa har avfärdat detta.",
        "it.a.site.sensor": "Kontrollera {sensor} på plats: kablage, kalibrering och om värdet har fastnat",
        "it.a.site.sensor.nosig": "Kontrollera de inblandade givarna på plats: kablage, kalibrering, värden som fastnat",
        "it.a.site.process": "Fråga operatörerna vad som ändrades vid {where}: inställningar, matning, utrustning",
        "it.a.site.data": "Kontrollera hur de här raderna sparades eller exporterades",
        "it.a.site.mixed": "Kontrollera först {sensor} och fråga sedan operatörerna vad som ändrades",
        "it.a.site.unknown": "Jämför {where} med operatörernas anteckningar från den tiden",
        "it.a.site.points": "Jämför raderna med operatörernas anteckningar och kontrollera de givare som förekommer oftast",
        "it.a.decide": "Godkänn, ifrågasätt eller rätta det här fyndet",
        "it.a.open.diagnosis": "Öppna det här fyndet", "it.a.open.flag": "Se det på tidslinjen", "it.a.open.check": "Se kontrollen i sin batch",
        "it.a.open.batch": "Öppna batchen och dess kontroller", "it.a.open.generic": "Öppna det på sin sida",
        "it.a.rows": "Se de här raderna på tidslinjen",
        "it.ask.diagnosis": "Vad bör jag kontrollera på plats för {id}?",
        "it.ask.flag": "Varför flaggades {id}, och vad bör jag kontrollera?",
        "it.ask.check": "Varför är {id} ett problem, och kan jag ändå använda de här raderna?",
        "it.ask.batch": "Kan jag lita på data i batch {id}?",
        "it.ask.generic": "Förklara {id} med enkla ord.",
        "it.fl.h.point": "Ett enstaka mätvärde passar inte in i sin omgivning: en störning eller en manipulation; enbart data kan inte avgöra det.",
        "it.fl.h.anomaly": "Ovanligt beteende hittades här, främst {sensor}.", "it.fl.h.anomaly.nosig": "Ovanligt beteende hittades här.",
        "it.fl.h.drift": "Processen gled långsamt bort från normal drift här, främst {sensor}.", "it.fl.h.drift.nosig": "Processen gled långsamt bort från normal drift här.",
        "it.fl.h.changepoint": "Processens beteende ändrades plötsligt här; {sensor} reagerade först.", "it.fl.h.changepoint.nosig": "Processens beteende ändrades plötsligt här.",
        "it.fl.h.cascade": "En störning spred sig från givare till givare här, med början vid {sensor}.", "it.fl.h.cascade.nosig": "En störning spred sig från givare till givare här.",
        "it.fl.h.dq": "Det här larmet gäller data, inte processen: {problem}, {sensor}.", "it.fl.h.dq.nosig": "Det här larmet gäller data, inte processen.",
        "it.fl.h.rule": "En av dina driftregler bröts här.",
        "it.fl.p.cause.process": "Troligaste orsak: en verklig förändring i processen ({sure}).", "it.fl.p.cause.sensor": "Troligaste orsak: en trasig givare ({sure}).",
        "it.fl.p.cause.data": "Troligaste orsak: ett problem i data ({sure}).", "it.fl.p.cause.mixed": "Troligen ligger mer än en orsak bakom ({sure}).",
        "it.fl.p.cause.unknown": "Orsaken gick inte att fastställa.",
        "it.fl.p.strength": "Det är {x} gånger starkare än något som setts under normal drift.",
        "it.ck.h.pass": "Den här kontrollen hittade inget fel.",
        "it.ck.h": "Dataproblem: {problem}, {sensor}.", "it.ck.h.nosig": "Dataproblem: {problem}.",
        "it.ck.p.use.pass": "De här raderna går att använda.",
        "it.ck.p.use.warn": "Raderna går ändå att använda, med viss försiktighet.",
        "it.ck.p.use.fail.sig": "De här raderna för den här givaren läggs åt sidan. De andra givarna går att använda.",
        "it.ck.p.use.fail.batch": "Hela batchen är inte betrodd, så var försiktig med fynd därifrån.",
        "it.ck.p.use.fail": "De drabbade raderna läggs åt sidan, så att de inte misstas för ett fel i processen.",
        "it.ck.a.sensor": "Kontrollera {sensor} på plats",
        "it.b.h.ok": "Data i batch {id} går att lita på.", "it.b.h.caution": "Data i batch {id} går att använda, med mindre problem.", "it.b.h.bad": "Data i batch {id} går inte att lita på.",
        "it.b.p.problems": "Hittade problem: {list}.", "it.b.p.sensors": "Berörda givare: {list}.", "it.b.p.score": "Den bedöms som {pct} pålitlig.",
        "it.b.p.none": "Alla kontroller gick igenom.",
        "it.ev.h": "Det här är ett uppmätt faktum som förklaringarna bygger på.",
        "it.unknown.h": "{id} hittades inte i den här körningen.",
        "pw.stuck": "frysta givarvärden", "pw.stale": "givare som slutat uppdateras", "pw.missing": "saknade värden", "pw.dropout": "givare som tystnat",
        "pw.out_of_range": "värden långt utanför det vanliga området", "pw.impossible_value": "omöjliga värden", "pw.unit_shift": "plötsliga skalförändringar (fel enhet eller decimaltecken)",
        "pw.saturation": "givare som fastnat vid sin gräns", "pw.quantization_change": "ändringar i hur noggrant värden sparas", "pw.sign_violation": "värden med fel tecken",
        "pw.relation_break": "givare som inte längre följer sina partner", "pw.local_spike": "enstaka udda mätvärden", "pw.out_of_order": "rader i fel tidsordning",
        "pw.duplicate": "dubblettrader", "pw.gap": "luckor i tiden", "pw.irregular_sampling": "ojämn tid mellan mätvärden", "pw.empty_rows": "tomma rader",
        "pw.redundancy_violation": "kopior som inte längre stämmer med sin källa", "pw.rule": "brutna driftregler", "pw.other": "andra dataproblem",
        "dq.h.bad.1": "En av {n} batchar går inte att lita på. Främsta orsaken: {top}.",
        "ov.p.data.bad.1": "Data: en av {n} batchar går inte att lita på, främst på grund av {top}.",
        "mon.p.points.1": "Dessutom hittades ett enstaka udda mätvärde. Det är en störning eller en manipulation; enbart data kan inte avgöra det.",
        "mon.p.dq.1": "Ytterligare ett larm gäller ett problem i data, inte i processen.",
        "as.p.thin.1": "Ett av {n} driftlägen syns sällan i data, så fel där är svårare att upptäcka.",
        "as.p.recs.1": "Systemet har ett förslag för att förbättra data. Inget ändras förrän du godkänner det.",
        "df.p.local.1": "AI-hjälpen som formulerar förklaringarna kördes på den här datorn en gång.",
        "df.h.sent.1": "En kort sammanfattning skickades till en extern AI-tjänst. En kopia av dina mätvärden lämnar aldrig den här datorn.",
        "df.h.blocked.1": "Dina data stannade på den här datorn. Ett försök att kontakta en extern AI-tjänst stoppades.",
        "lg.p.human.1": "En post är ett beslut som en människa har fattat (godkänn, ifrågasätt, rätta).",
        "lg.p.open.1": "Ett fynd väntar fortfarande på ett beslut av en människa.",
    },
}


def tr(lang: str, key: str, **kw: Any) -> str:
    """Template `key` in `lang` (English when the language lacks it); `key + '.1'` is used when n / k / b == 1."""
    one = any(kw.get(name) in (1, "1") for name in ("n", "k", "b"))
    keys = (key + ".1", key) if one else (key,)
    for table in (T.get(lang) or T["en"], T["en"]):
        for k in keys:
            s = table.get(k)
            if s is not None:
                try:
                    return s.format(**kw)
                except (KeyError, IndexError):
                    return s
    return key


# =====================================================================================================
# small helpers
# =====================================================================================================
def _lang(lang: Optional[str]) -> str:
    code = (lang or "en").lower()[:2]
    return code if code in LANGS else "en"


def _int(n: Any, lang: str) -> str:
    try:
        s = f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)
    return s if lang == "en" else s.replace(",", " ")


def _cnt(n: Any, lang: str) -> Any:
    """A count for a template: 1 stays the number 1 (selects the singular text), anything else is formatted."""
    try:
        return 1 if int(n) == 1 else _int(n, lang)
    except (TypeError, ValueError):
        return n


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f} %"
    except (TypeError, ValueError):
        return "–"


def _sure(c: Any, lang: str) -> str:
    try:
        v = float(c)
    except (TypeError, ValueError):
        return tr(lang, "sure.unsure")
    return tr(lang, "sure.very" if v >= 0.85 else "sure.fairly" if v >= 0.65 else "sure.notvery" if v >= 0.45 else "sure.unsure")


def _sure_sentence(c: Any, lang: str) -> str:
    """'the system is fairly sure' (a clause; capitalise it when it starts a sentence)."""
    word = _sure(c, lang)
    key = next((k for k in ("very", "fairly", "notvery", "unsure") if tr(lang, "sure." + k) == word), "unsure")
    return tr(lang, "sureS." + key)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _join(items: list[str], lang: str, max_items: int = 3) -> str:
    items = [i for i in items if i][:max_items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {tr(lang, 'and')} " + items[-1]


def _rows(a: Any, b: Any, lang: str) -> str:
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        return ""
    return tr(lang, "rows.1", a=_int(a, lang)) if a == b else tr(lang, "rows", a=_int(min(a, b), lang), b=_int(max(a, b), lang))


def _words(s: str) -> int:
    return len(s.split())


def _clip(s: str, max_words: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if _words(s) <= max_words:
        return s
    no_paren = re.sub(r"\s*\([^()]*\)", "", s)
    if _words(no_paren) <= max_words:
        return no_paren
    return " ".join(no_paren.split()[: max_words - 1]).rstrip(",;:–-") + " …"


def _pick(cands: list[str], max_words: int) -> str:
    cands = [c for c in cands if c]
    for c in cands:
        if _words(c) <= max_words:
            return c
    return _clip(cands[-1], max_words) if cands else ""


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([.,;:])", r"\1", s)
    return re.sub(r",\s*\.", ".", s)


def action(text: str, view: Optional[str] = None, ref: Optional[str] = None, ask: Optional[str] = None) -> dict[str, Any]:
    return {"text": _clip(text, 16), "view": view, "ref": ref, "ask": ask}


_CACHE: dict[tuple[str, str], tuple[tuple, Any]] = {}


def _memo(ws: Any, key: str, names: tuple[str, ...], build: Callable[[], Any]) -> Any:
    """Facts derived from artifacts, rebuilt only when one of the files changed."""
    stamp = []
    for n in names:
        try:
            st = (ws.dir / n).stat()
            stamp.append((n, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((n, 0, 0))
    k = (str(ws.dir), key)
    hit = _CACHE.get(k)
    if hit is not None and hit[0] == tuple(stamp):
        return hit[1]
    val = build()
    if len(_CACHE) > 96:
        _CACHE.clear()
    _CACHE[k] = (tuple(stamp), val)
    return val


def _signal_names(ws: Any) -> dict[str, str]:
    """{S01: 'Cooler current [A] (S01)'}: the operator's name or the header of the person's file, the short name in
    brackets. A file without a header has no names, so the short name stands alone."""
    def build() -> dict[str, str]:
        sigs = _read(ws, "signals.json", []) or []
        if isinstance(sigs, dict):
            sigs = sigs.get("signals") or []
        schema = _read(ws, "schema.json", {}) or {}
        had_header = schema.get("had_header", True) is not False
        out: dict[str, str] = {}
        for s in sigs:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            sid = str(s["id"])
            name = str(s.get("display_name") or (s.get("source_column") if had_header else "") or "").strip()
            if name and name != sid:
                out[sid] = f"{name if len(name) <= 28 else name[:27] + '…'} ({sid})"
            else:
                out[sid] = sid
        return out

    return _memo(ws, "names", ("signals.json", "schema.json"), build)


def _sensor(names: dict[str, str], sid: Any, lang: str) -> str:
    sid = str(sid or "")
    label = names.get(sid, sid)
    return tr(lang, "sensor", name=label) if label == sid else label


def _status(ws: Any) -> dict[str, Any]:
    st = _read(ws, "status.json", {}) or {}
    return st if isinstance(st, dict) else {}


STAGE_OF = {"understanding": "profile", "quality": "quality", "monitor": "detect", "diagnoses": "diagnose", "assessor": "assess", "report": "diagnose"}
FILES_OF = {"understanding": ("signals.json", "schema.json"), "quality": ("trust.jsonl", "checks.jsonl"), "monitor": ("detect_meta.json", "flags.jsonl"), "diagnoses": ("diagnoses.jsonl",), "assessor": ("assessor.json",), "report": ("diagnoses.jsonl",)}


def stage_state(ws: Any, view: str) -> str:
    """done | pending | failed | skipped for the pipeline stage a view depends on."""
    stage = STAGE_OF.get(view)
    if not stage:
        return "done"
    have = any((ws.dir / n).exists() for n in FILES_OF.get(view, ()))
    st = _status(ws)
    rec = next((s for s in (st.get("stages") or []) if isinstance(s, dict) and s.get("stage") == stage), None)
    if rec is None:
        return "done" if have else ("failed" if st.get("state") == "failed" else "pending")
    state = str(rec.get("state") or "pending")
    if state == "done":
        return "done"
    if state in ("failed", "skipped"):
        return state
    if st.get("state") == "failed":
        return "failed"
    if st.get("state") == "done" and have:
        return "done"
    return "pending"


def _not_ready(state: str, lang: str) -> dict[str, Any]:
    if state == "failed":
        return {"verdict": "pending", "headline": tr(lang, "failed.h"), "points": [tr(lang, "failed.p")], "actions": [action(tr(lang, "failed.a"), "runs")]}
    if state == "skipped":
        return {"verdict": "pending", "headline": tr(lang, "skipped.h"), "points": [], "actions": [action(tr(lang, "pending.a"), "runs")]}
    return {"verdict": "pending", "headline": tr(lang, "pending.h"), "points": [tr(lang, "pending.p")], "actions": [action(tr(lang, "pending.a"), "runs")]}


# =====================================================================================================
# facts (language independent, cached per run)
# =====================================================================================================
_DUP = {"duplicate", "duplicate_rows", "duplicate_key", "duplicate_timestamp", "duplicate_ts"}
_PROBLEMS = {"stuck", "stale", "missing", "dropout", "out_of_range", "impossible_value", "unit_shift", "saturation", "quantization_change", "sign_violation", "relation_break", "local_spike", "out_of_order", "gap", "irregular_sampling", "empty_rows", "redundancy_violation"}


def problem_key(check_type: Any) -> Optional[str]:
    ct = str(check_type or "")
    if not ct or ct.endswith("_ok"):
        return None
    if ct.startswith("rule"):
        return "rule"
    if ct in _DUP:
        return "duplicate"
    return ct if ct in _PROBLEMS else "other"


def _facts_understanding(ws: Any) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        schema = _read(ws, "schema.json", {}) or {}
        sigs = _read(ws, "signals.json", []) or []
        if isinstance(sigs, dict):
            sigs = sigs.get("signals") or []
        U = _read(ws, "understanding.json", {}) or {}
        ds = U.get("dataset") if isinstance(U.get("dataset"), dict) else {}
        infs = _read(ws, "inferences.jsonl", []) or []
        lk = ds.get("domain_likelihood") or schema.get("domain_likelihood") or {}
        return {
            "rows": ds.get("n_rows") or schema.get("n_rows"), "n_sig": len(sigs) or len(schema.get("signal_columns") or []),
            "groups": ds.get("n_groups") or schema.get("n_groups") or 1, "period": ds.get("sample_period_seconds") or schema.get("sample_period_seconds"),
            "excluded": sum(1 for s in sigs if isinstance(s, dict) and s.get("excluded")),
            "unsure": sum(1 for i in infs if isinstance(i, dict) and i.get("subject") == "dataset" and i.get("status") in ("uncertain", "assumed")),
            "sensor_like": lk.get("sensor_stream") if isinstance(lk, dict) else None,
            "first_signal": next((str(s["id"]) for s in sigs if isinstance(s, dict) and s.get("id") and not s.get("excluded")), None),
        }

    return _memo(ws, "f_und", ("schema.json", "signals.json", "understanding.json", "inferences.jsonl"), build)


def _facts_quality(ws: Any) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        trust = [t for t in (_read(ws, "trust.jsonl", []) or []) if isinstance(t, dict)]
        checks = [c for c in (_read(ws, "checks.jsonl", []) or []) if isinstance(c, dict)]
        issue = lambda t: bool(t.get("reasons") or t.get("local_untrusted") or t.get("untrusted_signals"))  # noqa: E731
        bad = [t for t in trust if not t.get("trusted")]
        caution = [t for t in trust if t.get("trusted") and issue(t)]
        weight: Counter = Counter()
        count: Counter = Counter()
        per_sensor: Counter = Counter()
        top_check, top_sev = None, -1.0
        bad_ids = {t.get("batch_id") for t in bad}
        by_batch: dict[str, Counter] = {}
        for c in checks:
            status = c.get("status")
            key = problem_key(c.get("check_type"))
            if key is None or status not in ("fail", "warn"):
                continue
            sev = float(c.get("severity") or 0)
            if c.get("batch_id"):
                by_batch.setdefault(str(c["batch_id"]), Counter())[key] += sev * (4.0 if status == "fail" else 1.0)
            weight[key] += sev * (2.0 if status == "fail" else 1.0)
            count[key] += 1
            if status == "fail":
                for s in c.get("signals") or []:
                    per_sensor[str(s)] += 1
                rank = sev + (1.0 if c.get("batch_id") in bad_ids else 0.0)
                if rank > top_sev:
                    top_check, top_sev = c.get("check_id"), rank
        worst = sorted(bad or caution, key=lambda t: (t.get("trust_score") if t.get("trust_score") is not None else 1.0, str(t.get("batch_id"))))
        return {"n": len(trust), "n_bad": len(bad), "n_caution": len(caution), "problems": [(k, count[k]) for k, _ in weight.most_common(4)],
                "sensors": [s for s, _ in per_sensor.most_common(3)], "worst_batch": worst[0].get("batch_id") if worst else None, "top_check": top_check,
                "mean_trust": (sum(float(t.get("trust_score") or 0) for t in trust) / len(trust)) if trust else None, "have": bool(trust or checks),
                "by_batch": {b: [k for k, _ in cnt.most_common(3)] for b, cnt in by_batch.items()}}

    return _memo(ws, "f_dq", ("trust.jsonl", "checks.jsonl"), build)


_SUSTAINED = ("anomaly", "drift", "changepoint", "cascade", "rule")


def _facts_monitor(ws: Any) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        flags = [f for f in (_read(ws, "flags.jsonl", []) or []) if isinstance(f, dict)]
        sus = _read(ws, "suspicious_rows.json", {}) or {}
        gs = _read(ws, "group_scores.json", {}) or {}
        meta = _read(ws, "detect_meta.json", {}) or {}
        schema = _read(ws, "schema.json", {}) or {}
        sustained = sorted((f for f in flags if f.get("kind") in _SUSTAINED and f.get("row_start") is not None), key=lambda f: (str(f.get("group_id")), int(f.get("row_start") or 0)))
        places: list[dict[str, Any]] = []
        for f in sustained:
            a, b = int(f.get("row_start") or 0), int(f.get("row_end") if f.get("row_end") is not None else f.get("row_start") or 0)
            last = places[-1] if places else None
            if last is not None and last["group"] == str(f.get("group_id")) and a <= last["b"] + 1:
                last["b"] = max(last["b"], b)
                if float(f.get("severity") or 0) > float(last["best"].get("severity") or 0):
                    last["best"] = f
            else:
                places.append({"group": str(f.get("group_id")), "a": a, "b": b, "best": f})
        top = max((p["best"] for p in places), key=lambda f: (float(f.get("severity") or 0), float(f.get("confidence") or 0)), default=None)
        n_points = int(sus.get("n_rows") or 0) if isinstance(sus, dict) and sus.get("n_rows") else sum(1 for f in flags if f.get("kind") == "point")
        ev = meta.get("events") if isinstance(meta.get("events"), dict) else {}
        n_groups = ev.get("n_groups") or gs.get("n_groups") or schema.get("n_groups") or 1
        return {"n_places": len(places), "causes": Counter(str(p["best"].get("likely_cause_class") or "unknown") for p in places), "top": top,
                "n_points": n_points, "n_dq": sum(1 for f in flags if f.get("kind") == "dq"), "n_groups": n_groups,
                "groups_hit": len({p["group"] for p in places}), "has_sus": bool(isinstance(sus, dict) and sus.get("n_rows")), "have": bool(meta or flags)}

    return _memo(ws, "f_mon", ("flags.jsonl", "suspicious_rows.json", "group_scores.json", "detect_meta.json", "schema.json"), build)


def _is_points_diag(d: dict[str, Any], flags_by_id: dict[str, dict[str, Any]]) -> bool:
    fl = [flags_by_id[i] for i in (d.get("flag_ids") or []) if i in flags_by_id]
    if fl:
        return all(f.get("kind") == "point" for f in fl)
    return "isolated" in str(d.get("fault_type") or "").lower()


def _diag_place(d: dict[str, Any], flags_by_id: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    fl = [flags_by_id[i] for i in (d.get("flag_ids") or []) if i in flags_by_id and flags_by_id[i].get("row_start") is not None]
    return max(fl, key=lambda f: float(f.get("severity") or 0), default=None)


def _facts_diagnoses(ws: Any) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        diags = [d for d in (_read(ws, "diagnoses.jsonl", []) or []) if isinstance(d, dict)]
        flags_by_id = {f.get("id"): f for f in (_read(ws, "flags.jsonl", []) or []) if isinstance(f, dict)}
        sev = lambda d: max((float(flags_by_id[i].get("severity") or 0) for i in (d.get("flag_ids") or []) if i in flags_by_id), default=0.0)  # noqa: E731
        verdict = lambda d: str((d.get("critique") or {}).get("verdict") or "") if isinstance(d.get("critique"), dict) else ""  # noqa: E731
        weight = {"process": 1.0, "sensor": 1.0, "mixed": 1.0, "data": 0.8}
        ranked = sorted(diags, key=lambda d: (verdict(d) == "rejected", -sev(d) * weight.get(str(d.get("cause_class")), 0.7), -float(d.get("confidence") or 0)))
        top = ranked[0] if ranked else None
        return {"n": len(diags), "top": top, "top_points": bool(top and _is_points_diag(top, flags_by_id)), "top_place": _diag_place(top, flags_by_id) if top else None,
                "top_n_flags": len(top.get("flag_ids") or []) if top else 0, "top_sev": sev(top) if top else 0.0,
                "causes": Counter(str(d.get("cause_class") or "unknown") for d in diags), "critique": Counter(verdict(d) for d in diags),
                "reviewed": sum(1 for d in diags if d.get("human_status")), "exists": (ws.dir / "diagnoses.jsonl").exists()}

    return _memo(ws, "f_dg", ("diagnoses.jsonl", "flags.jsonl"), build)


# =====================================================================================================
# views
# =====================================================================================================
def _period_words(seconds: Any, lang: str) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    unit, n = ("h", s / 3600) if s >= 5400 else ("min", s / 60) if s >= 90 else ("s", s)
    n = int(round(n)) if n >= 1 else round(n, 1)
    return tr(lang, f"unit.{unit}", n=n)


def _brief_understanding(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    F = _facts_understanding(ws)
    rows, n = _int(F["rows"], lang) if F["rows"] else "?", F["n_sig"]
    odd = isinstance(F["sensor_like"], (int, float)) and F["sensor_like"] < 0.4
    if odd:
        head = tr(lang, "und.h.odd")
    else:
        head = tr(lang, "und.h.groups", rows=rows, n=n, g=_int(F["groups"], lang)) if (F["groups"] or 1) > 1 else tr(lang, "und.h.ok", rows=rows, n=n)
    period = _period_words(F["period"], lang)
    points = [tr(lang, "und.p.period", period=period) if period else tr(lang, "und.p.noperiod")]
    if F["excluded"]:
        points.append(tr(lang, "und.p.excluded", k=_cnt(F["excluded"], lang), n=n))
    points.append(tr(lang, "und.p.names"))
    if F["unsure"] and len(points) < MAX_POINTS:
        points.append(tr(lang, "und.p.unsure", k=_cnt(F["unsure"], lang)))
    acts = [action(tr(lang, "und.a.check"), "understanding", "section:catalog")]
    if F["first_signal"]:
        acts.append(action(tr(lang, "und.a.name"), "understanding", F["first_signal"]))
    acts.append(action(tr(lang, "und.ask"), ask=tr(lang, "und.ask")))
    return {"verdict": "attention" if odd else "ok", "headline": head, "points": points, "actions": acts}


def _problem_list(problems: list[tuple[str, int]], lang: str, limit: int = 3, counts: bool = True) -> str:
    return _join([tr(lang, "pw." + k) + (f" ({_int(c, lang)})" if counts else "") for k, c in problems[:limit]], lang, limit)


def _quality_core(ws: Any, lang: str) -> dict[str, Any]:
    F = _facts_quality(ws)
    top = tr(lang, "pw." + F["problems"][0][0]) if F["problems"] else tr(lang, "pw.other")
    level = "bad" if F["n_bad"] else "caution" if F["n_caution"] else "ok"
    k = F["n_bad"] or F["n_caution"]
    return {"F": F, "level": level, "k": k, "top": top}


def _brief_quality(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    Q = _quality_core(ws, lang)
    F, level = Q["F"], Q["level"]
    names = _signal_names(ws)
    n = _int(F["n"], lang)
    if level == "ok":
        head = tr(lang, "dq.h.ok", n=n)
    else:
        head = _pick([tr(lang, "dq.h." + level, k=_int(Q["k"], lang), n=n, top=Q["top"]), tr(lang, "dq.h." + level, k=_int(Q["k"], lang), n=n, top=re.sub(r"\s*\([^()]*\)", "", Q["top"]))], MAX_HEADLINE_WORDS)
    points = []
    if len(F["problems"]) > 1 or (F["problems"] and level == "ok"):
        points.append(_pick([tr(lang, "dq.p.top", list=_problem_list(F["problems"], lang, 3)), tr(lang, "dq.p.top", list=_problem_list(F["problems"], lang, 2)), tr(lang, "dq.p.top", list=_problem_list(F["problems"], lang, 2, counts=False))], MAX_POINT_WORDS))
    if F["sensors"]:
        points.append(_pick([tr(lang, "dq.p.sensors", list=_join([names.get(s, s) for s in F["sensors"]], lang, m)) for m in (3, 2, 1)], MAX_POINT_WORDS))
    points.append(tr(lang, "dq.p.setaside") if level != "ok" else tr(lang, "dq.p.batch"))
    acts = []
    if F["worst_batch"] and level != "ok":
        acts.append(action(tr(lang, "dq.a.worst", id=F["worst_batch"]), "quality", F["worst_batch"]))
    if F["top_check"]:
        acts.append(action(tr(lang, "dq.a.check"), "quality", F["top_check"]))
    if not acts:
        acts.append(action(tr(lang, "dq.a.checks"), "quality", "section:checks"))
    acts.append(action(tr(lang, "dq.ask"), ask=tr(lang, "dq.ask")))
    return {"verdict": {"ok": "ok", "caution": "attention", "bad": "problem"}[level], "headline": head, "points": points, "actions": acts}


def _flag_where(f: dict[str, Any], lang: str) -> str:
    return _rows(f.get("row_start"), f.get("row_end") if f.get("row_end") is not None else f.get("row_start"), lang)


def _top_signal(obj: dict[str, Any], key: str) -> Optional[str]:
    for r in obj.get(key) or []:
        if isinstance(r, dict) and r.get("signal"):
            return str(r["signal"])
    return None


def _monitor_headline(ws: Any, lang: str) -> tuple[str, str]:
    """(verdict, headline) of the monitor; shared with the overview."""
    F = _facts_monitor(ws)
    names = _signal_names(ws)
    if F["n_places"]:
        top = F["top"] or {}
        where = _flag_where(top, lang)
        sig = _top_signal(top, "signals_ranked")
        k = _cnt(F["n_places"], lang)
        cands = []
        if sig:
            cands.append(tr(lang, "mon.h.events", k=k, where=where, sensor=_sensor(names, sig, lang)))
        cands.append(tr(lang, "mon.h.events.short", k=k, where=where))
        verdict = "problem" if float(top.get("severity") or 0) >= 0.7 else "attention"
        return verdict, _pick(cands, MAX_HEADLINE_WORDS)
    if F["n_points"]:
        return "attention", tr(lang, "mon.h.points", n=_cnt(F["n_points"], lang))
    return "ok", tr(lang, "mon.h.none")


def _brief_monitor(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    F = _facts_monitor(ws)
    verdict, head = _monitor_headline(ws, lang)
    points: list[str] = []
    acts: list[dict[str, Any]] = []
    if F["n_places"]:
        top = F["top"] or {}
        where = _flag_where(top, lang)
        cause_bits = [tr(lang, "mon.cause." + (c if c in ("process", "sensor", "data", "mixed") else "unknown"), n=_cnt(n, lang)) for c, n in F["causes"].most_common(3)]
        points.append(tr(lang, "mon.p.causes", list=_join(cause_bits, lang, 3)))
        if (F["n_groups"] or 1) > 1:
            points.append(tr(lang, "mon.p.groups", k=_int(F["groups_hit"], lang), g=_int(F["n_groups"], lang)))
        if F["n_points"]:
            points.append(tr(lang, "mon.p.points", n=_cnt(F["n_points"], lang)))
        elif F["n_dq"]:
            points.append(tr(lang, "mon.p.dq", n=_cnt(F["n_dq"], lang)))
        acts.append(action(tr(lang, "mon.a.top", where=where), "monitor", top.get("id")))
        acts.append(action(tr(lang, "mon.a.diag"), "diagnoses", None))
        acts.append(action(tr(lang, "mon.ask", where=where), ask=tr(lang, "mon.ask", where=where)))
    elif F["n_points"]:
        points.append(tr(lang, "mon.p.points.only"))
        if F["n_dq"]:
            points.append(tr(lang, "mon.p.dq", n=_int(F["n_dq"], lang)))
        points.append(tr(lang, "mon.p.learned"))
        acts.append(action(tr(lang, "mon.a.sus"), "monitor", "section:suspicious" if F["has_sus"] else "section:flags"))
        acts.append(action(tr(lang, "mon.a.diag"), "diagnoses", None))
        acts.append(action(tr(lang, "mon.ask.points"), ask=tr(lang, "mon.ask.points")))
    else:
        if F["n_dq"]:
            points.append(tr(lang, "mon.p.dq", n=_int(F["n_dq"], lang)))
        points.append(tr(lang, "mon.p.learned"))
        acts.append(action(tr(lang, "mon.a.chart"), "monitor", "section:timeline"))
        acts.append(action(tr(lang, "mon.ask.none"), ask=tr(lang, "mon.ask.none")))
    return {"verdict": verdict, "headline": head, "points": points, "actions": acts}


def _diag_what_where(ws: Any, lang: str) -> tuple[str, str]:
    F = _facts_diagnoses(ws)
    d = F["top"] or {}
    names = _signal_names(ws)
    cause = str(d.get("cause_class") or "unknown")
    sig = _top_signal(d, "ranked_signals")
    if F["top_points"]:
        what = tr(lang, "dg.what.points", n=_int(F["top_n_flags"] or 1, lang))
        return what, ""
    if cause == "sensor":
        what = tr(lang, "dg.what.sensor", sensor=_sensor(names, sig, lang)) if sig else tr(lang, "dg.what.sensor.nosig")
    else:
        what = tr(lang, "dg.what." + (cause if cause in ("process", "data", "mixed") else "unknown"))
    place = F["top_place"]
    rows = _flag_where(place, lang) if place else ""
    g = d.get("group_id")
    multi = (_facts_monitor(ws)["n_groups"] or 1) > 1
    if rows and g not in (None, "") and multi:
        where = tr(lang, "dg.where.both", g=g, rows=rows)
    elif rows:
        where = tr(lang, "dg.where.rows", rows=rows)
    elif g not in (None, "") and multi:
        where = tr(lang, "dg.where.group", g=g)
    else:
        where = ""
    return what, where


def _brief_diagnoses(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    F = _facts_diagnoses(ws)
    if not F["n"]:
        return {"verdict": "ok", "headline": tr(lang, "dg.h.none"), "points": [tr(lang, "mon.p.learned")], "actions": [action(tr(lang, "mon.a.chart"), "monitor", "section:timeline"), action(tr(lang, "mon.ask.none"), ask=tr(lang, "mon.ask.none"))]}
    d = F["top"]
    what, where = _diag_what_where(ws, lang)
    sure = _cap(_sure_sentence(d.get("confidence"), lang))
    explained = str(d.get("cause_class") or "unknown") in ("process", "sensor", "data", "mixed") and not F["top_points"]
    head = _pick([_tidy(tr(lang, "dg.h.top", what=what, where=where, sure=sure)) if explained else "", _tidy(tr(lang, "dg.h.top.short", what=what, where=where)), _tidy(tr(lang, "dg.h.top.min", what=what.rstrip(",")))], MAX_HEADLINE_WORDS)
    norm = lambda c: c if c in ("process", "sensor", "data", "mixed") else "unknown"  # noqa: E731
    if F["n"] == 1:
        points = [tr(lang, "dg.one." + norm(next(iter(F["causes"]), "unknown")))]
    else:
        bits = [tr(lang, "dg.count." + norm(c), n=_cnt(n, lang)) for c, n in F["causes"].most_common(4)]
        points = [tr(lang, "dg.p.count", n=_int(F["n"], lang), list=_join(bits, lang, 4))]
    cr = F["critique"]
    checked = [(v, cr.get(v, 0)) for v in ("supported", "weakened", "rejected") if cr.get(v)]
    if checked and len(checked) == 1 and checked[0][0] == "supported":
        points.append(tr(lang, "dg.p.challenge.all", n=_cnt(checked[0][1], lang)))
    elif checked:
        points.append(tr(lang, "dg.p.challenge", list=_join([tr(lang, "dg.ch." + v, n=_cnt(c, lang)) for v, c in checked], lang, 3)))
    points.append(tr(lang, "dg.p.review", k=_int(F["reviewed"], lang), n=_int(F["n"], lang)) if F["reviewed"] else tr(lang, "dg.p.review.none"))
    cause = str(d.get("cause_class") or "unknown")
    serious = cause in ("process", "sensor", "mixed") and float(d.get("confidence") or 0) >= 0.65 and not F["top_points"]
    acts = [action(tr(lang, "dg.a.open"), "diagnoses", d.get("id")), action(tr(lang, "dg.ask", id=d.get("id")), ask=tr(lang, "dg.ask", id=d.get("id")))]
    return {"verdict": "problem" if serious else "attention", "headline": head, "points": points, "actions": acts}


def _brief_assessor(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    a = _read(ws, "assessor.json", {}) or {}
    score = a.get("combined_score", a.get("score"))
    if isinstance(score, (int, float)):
        level = "good" if score >= 0.8 else "fair" if score >= 0.6 else "weak"
        head = tr(lang, "as.h." + level, pct=_pct(score))
    else:
        level, head = "fair", tr(lang, "as.h.noscore")
    tri = lambda v: "yes" if v is True else "no" if v is False else "unclear"  # noqa: E731
    more = a.get("more_data_verdict") if isinstance(a.get("more_data_verdict"), dict) else {}
    less = a.get("less_data_verdict") if isinstance(a.get("less_data_verdict"), dict) else {}
    points = [tr(lang, "as.p.more." + tri(more.get("would_help"))), tr(lang, "as.p.less." + tri(less.get("would_help")))]
    cov = a.get("coverage") if isinstance(a.get("coverage"), dict) else {}
    thin, regimes = cov.get("thin_regimes") or [], cov.get("regimes") or []
    recs = a.get("recommendations") or []
    if thin and regimes:
        points.append(tr(lang, "as.p.thin", k=len(thin), n=len(regimes)))
    elif recs:
        points.append(tr(lang, "as.p.recs", n=len(recs)))
    acts = []
    if recs:
        acts.append(action(tr(lang, "as.a.recs"), "assessor", "section:recommendations"))
    acts.append(action(tr(lang, "as.ask"), ask=tr(lang, "as.ask")))
    acts.append(action(tr(lang, "as.ask2"), ask=tr(lang, "as.ask2")))
    return {"verdict": {"good": "ok", "fair": "attention", "weak": "problem"}[level], "headline": head, "points": points, "actions": acts}


def _brief_dataflow(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    recs = [r for r in (_read(ws, "egress_ledger.jsonl", []) or []) if isinstance(r, dict)]
    sent = sum(1 for r in recs if r.get("route") == "external" and r.get("guard_result") == "allowed" and r.get("ok", True))
    blocked = sum(1 for r in recs if r.get("guard_result") == "blocked")
    local = sum(1 for r in recs if r.get("route") == "local")
    try:
        allow = bool(settings.active_profile.allow_external)
    except Exception:
        allow = False
    if sent:
        verdict, head = "attention", tr(lang, "df.h.sent", n=_int(sent, lang))
    elif blocked:
        verdict, head = "ok", tr(lang, "df.h.blocked", b=_int(blocked, lang))
    else:
        verdict, head = "ok", tr(lang, "df.h.local")
    points = [tr(lang, "df.p.local", n=_int(local, lang)) if local else tr(lang, "df.p.nolocal"), tr(lang, "df.p.mode.on" if allow else "df.p.mode.off"), tr(lang, "df.p.list")]
    acts = [action(tr(lang, "df.a.list"), "dataflow", "section:ledger"), action(tr(lang, "df.a.mode"), "dataflow", "section:profile"), action(tr(lang, "df.ask"), ask=tr(lang, "df.ask"))]
    return {"verdict": verdict, "headline": head, "points": points, "actions": acts}


def _brief_log(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    n, humans, chain = 0, 0, None
    try:
        n = int(ws.log.count())
        humans = len(ws.log.entries(actor_prefix="human:", limit=100_000))
        if n <= 20_000:
            chain = ws.log.verify_chain()
    except Exception:
        pass
    if chain is not None and not chain.get("ok"):
        verdict, head = "problem", tr(lang, "lg.h.bad", seq=chain.get("first_bad_seq"))
    else:
        verdict, head = "ok", tr(lang, "lg.h.ok" if chain is not None else "lg.h.plain", n=_int(n, lang))
    D = _facts_diagnoses(ws)
    open_n = max(0, D["n"] - D["reviewed"])
    points = [tr(lang, "lg.p.human", k=_int(humans, lang)) if humans else tr(lang, "lg.p.human.none")]
    if open_n:
        points.append(tr(lang, "lg.p.open", k=_int(open_n, lang)))
    points.append(tr(lang, "lg.p.why"))
    acts = [action(tr(lang, "lg.a.verify"), "log", "section:verify")]
    if open_n:
        acts.append(action(tr(lang, "lg.a.review"), "diagnoses", None))
    acts.append(action(tr(lang, "lg.ask"), ask=tr(lang, "lg.ask")))
    return {"verdict": verdict, "headline": head, "points": points, "actions": acts}


def _brief_report(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    ready = any((ws.dir / f"report_{code}.html").exists() for code in LANGS)
    points = [tr(lang, "rp.p.content"), tr(lang, "rp.p.formats"), tr(lang, "rp.p.local")]
    acts = [action(tr(lang, "rp.a.open"), "report", "section:preview"), action(tr(lang, "rp.a.send"), "report", "section:email"), action(tr(lang, "rp.ask"), ask=tr(lang, "rp.ask"))]
    return {"verdict": "ok", "headline": tr(lang, "rp.h.ready" if ready else "rp.h.can"), "points": points, "actions": acts}


_RANK = {"ok": 0, "pending": 0, "attention": 1, "problem": 2}


def _brief_overview(ws: Any, settings: Any, lang: str) -> dict[str, Any]:
    st = _status(ws)
    stages = [s for s in (st.get("stages") or []) if isinstance(s, dict)]
    state = str(st.get("state") or "")
    q_done, m_done, d_done = (stage_state(ws, v) == "done" for v in ("quality", "monitor", "diagnoses"))
    if state == "failed" and not (q_done and m_done):
        failed = next((s.get("stage") for s in stages if s.get("state") == "failed"), None)
        head = tr(lang, "ov.h.failed", stage=tr(lang, "stage." + str(failed)) if failed else "?") if failed else tr(lang, "failed.h")
        return {"verdict": "problem", "headline": head, "points": [tr(lang, "failed.p")], "actions": [action(tr(lang, "failed.a"), "runs")]}
    if state in ("running", "pending") and not (q_done and m_done and d_done):
        done = sum(1 for s in stages if s.get("state") == "done")
        points = []
        if q_done:
            points.append(_overview_data_point(ws, lang))
        if m_done:
            points.append(_monitor_headline(ws, lang)[1])
        return {"verdict": "pending", "headline": tr(lang, "ov.h.running", done=done, total=len(stages) or 7), "points": points or [tr(lang, "pending.p")], "actions": [action(tr(lang, "ov.a.progress"), "runs")]}
    if not (q_done or m_done or d_done):
        return _not_ready("pending", lang)
    # finished (or an older run without a stage record): usable data? anything wrong? what first?
    verdicts, points, acts = [], [], []
    data_part, proc_part = tr(lang, "ov.data.pending"), tr(lang, "ov.proc.pending")
    Q = _quality_core(ws, lang) if q_done else None
    if Q is not None:
        data_part = tr(lang, "ov.data." + Q["level"])
        verdicts.append({"ok": "ok", "caution": "attention", "bad": "problem"}[Q["level"]])
        points.append(_overview_data_point(ws, lang))
    M = _facts_monitor(ws) if m_done else None
    if M is not None:
        mv, mh = _monitor_headline(ws, lang)
        verdicts.append(mv)
        proc_part = tr(lang, "ov.proc.events", k=_cnt(M["n_places"], lang)) if M["n_places"] else tr(lang, "ov.proc.points", n=_cnt(M["n_points"], lang)) if M["n_points"] else tr(lang, "ov.proc.none")
        points.append(mh)
    D = _facts_diagnoses(ws) if d_done else None
    if D is not None and D["n"]:
        what, where = _diag_what_where(ws, lang)
        points.append(_pick([_tidy(tr(lang, "ov.p.first", what=what, where=where)), _tidy(tr(lang, "ov.p.first.min", what=what.rstrip(",")))], MAX_POINT_WORDS))
        verdicts.append(_brief_diagnoses(ws, settings, lang)["verdict"])
        acts.append(action(tr(lang, "ov.a.diag"), "diagnoses", D["top"].get("id")))
    if Q is not None and Q["level"] == "bad" and Q["F"]["worst_batch"]:
        acts.insert(0, action(tr(lang, "ov.a.batch", id=Q["F"]["worst_batch"]), "quality", Q["F"]["worst_batch"]))
    if M is not None and M["n_points"] and M["has_sus"] and len(acts) < 2:
        acts.append(action(tr(lang, "ov.a.sus"), "monitor", "section:suspicious"))
    if len(acts) < MAX_ACTIONS and d_done:
        acts.append(action(tr(lang, "ov.a.report"), "report", None))
    if len(acts) < MAX_ACTIONS:
        acts.append(action(tr(lang, "ov.ask"), ask=tr(lang, "ov.ask")))
    verdict = max(verdicts, key=lambda v: _RANK.get(v, 0)) if verdicts else "ok"
    return {"verdict": verdict, "headline": tr(lang, "ov.h", data=data_part, proc=proc_part), "points": points, "actions": acts}


def _overview_data_point(ws: Any, lang: str) -> str:
    Q = _quality_core(ws, lang)
    n = _int(Q["F"]["n"], lang)
    if Q["level"] == "ok":
        return tr(lang, "ov.p.data.ok", n=n)
    return _pick([tr(lang, "ov.p.data." + Q["level"], k=_int(Q["k"], lang), n=n, top=Q["top"]), tr(lang, "ov.p.data." + Q["level"], k=_int(Q["k"], lang), n=n, top=re.sub(r"\s*\([^()]*\)", "", Q["top"]))], MAX_POINT_WORDS)


_BUILDERS: dict[str, Callable[[Any, Any, str], dict[str, Any]]] = {
    "understanding": _brief_understanding, "quality": _brief_quality, "monitor": _brief_monitor, "diagnoses": _brief_diagnoses,
    "assessor": _brief_assessor, "dataflow": _brief_dataflow, "log": _brief_log, "report": _brief_report, "overview": _brief_overview,
}


def _finish(out: dict[str, Any]) -> dict[str, Any]:
    out["headline"] = _clip(out.get("headline") or "", MAX_HEADLINE_WORDS)
    out["points"] = [_clip(p, MAX_POINT_WORDS) for p in (out.get("points") or []) if p][:MAX_POINTS]
    acts = []
    for a in out.get("actions") or []:
        if a and a.get("text") and (a.get("view") or a.get("ask")):
            acts.append({"text": a["text"], "view": a.get("view"), "ref": a.get("ref"), "ask": a.get("ask")})
    out["actions"] = acts[:MAX_ACTIONS]
    return out


def brief_for(ws: Any, settings: Any, view: str, lang: str = "en") -> dict[str, Any]:
    """Summary of one view (or of the whole run: view='overview'). Never raises for a known view."""
    lang = _lang(lang)
    if view not in _BUILDERS:
        raise ValueError(f"unknown view {view!r}")
    state = stage_state(ws, view)
    try:
        body = _BUILDERS[view](ws, settings, lang) if state == "done" else _not_ready(state, lang)
    except Exception:
        body = _not_ready("pending", lang)
    out = _finish(body)
    return {"view": view, "language": lang, "verdict": out["verdict"], "headline": out["headline"], "points": out["points"], "actions": out["actions"], "source": "template"}


# =====================================================================================================
# items
# =====================================================================================================
def _short_time(ts: Any) -> str:
    s = str(ts or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s)
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _where_point(obj: dict[str, Any], lang: str, multi_group: bool, batch: bool = False) -> str:
    a = obj.get("row_start")
    if a is None:
        return ""
    rows = _rows(a, obj.get("row_end") if obj.get("row_end") is not None else a, lang)
    if batch and obj.get("batch_id"):
        where = tr(lang, "it.where.batch", batch=obj.get("batch_id"), rows=rows)
    elif multi_group and obj.get("group_id") not in (None, ""):
        where = tr(lang, "it.where.group", g=obj.get("group_id"), rows=rows)
    else:
        where = tr(lang, "it.where.rows", rows=rows)
    t0, t1 = _short_time(obj.get("time_start")), _short_time(obj.get("time_end"))
    return tr(lang, "it.p.where.time", where=where, t0=t0, t1=t1) if t0 and t1 else tr(lang, "it.p.where", where=where)


def _sure_point(conf: Any, verdict: str, lang: str) -> str:
    key = "it.p.sure." + verdict if verdict in ("weakened", "rejected") else "it.p.sure"
    return tr(lang, key, sure=_sure_sentence(conf, lang), pct=_pct(conf))


def _rows_ref(obj: dict[str, Any], signals: list[str]) -> Optional[str]:
    a = obj.get("row_start")
    if a is None:
        return None
    b = obj.get("row_end") if obj.get("row_end") is not None else a
    return f"rows:{int(a)}-{int(b)}" + (":" + ",".join(signals[:4]) if signals else "")


def _item_diagnosis(ws: Any, d: dict[str, Any], lang: str) -> dict[str, Any]:
    names = _signal_names(ws)
    flags_by_id = {f.get("id"): f for f in (_read(ws, "flags.jsonl", []) or []) if isinstance(f, dict)}
    multi = (_facts_monitor(ws)["n_groups"] or 1) > 1
    cause = str(d.get("cause_class") or "unknown")
    cause = cause if cause in ("process", "sensor", "data", "mixed") else "unknown"
    sigs = [str(r["signal"]) for r in (d.get("ranked_signals") or []) if isinstance(r, dict) and r.get("signal")]
    sensor = _sensor(names, sigs[0], lang) if sigs else ""
    verdict = str((d.get("critique") or {}).get("verdict") or "") if isinstance(d.get("critique"), dict) else ""
    points_kind = _is_points_diag(d, flags_by_id)
    place = _diag_place(d, flags_by_id)
    where_rows = _flag_where(place, lang) if place else ""
    if points_kind:
        n = len(d.get("flag_ids") or []) or 1
        head = tr(lang, "it.dg.h.points", n=_int(n, lang))
        where_pt = tr(lang, "it.p.where.points", n=_int(n, lang))
        site = tr(lang, "it.a.site.points")
    else:
        if cause == "sensor":
            head = tr(lang, "it.dg.h.sensor", sensor=sensor) if sensor else tr(lang, "it.dg.h.sensor.nosig")
        elif cause == "data" and sensor:
            head = _pick([tr(lang, "it.dg.h.data.sig", sensor=sensor), tr(lang, "it.dg.h.data")], MAX_HEADLINE_WORDS)
        else:
            head = tr(lang, "it.dg.h." + cause)
        if not (sensor and head.startswith(sensor)):   # a sensor's own name keeps its spelling ("xmv_3 (S44) is ...")
            head = head[0].upper() + head[1:]
        where_pt = _where_point({**place, "group_id": d.get("group_id", place.get("group_id"))}, lang, multi) if place else ""
        if cause in ("sensor", "mixed"):
            site = tr(lang, "it.a.site." + cause, sensor=sensor) if sensor else tr(lang, "it.a.site.sensor.nosig")
        elif cause == "data":
            site = tr(lang, "it.a.site.data")
        else:
            site = tr(lang, "it.a.site." + cause, where=where_rows) if where_rows else tr(lang, "it.a.site.sensor.nosig")
    points = [_sure_point(d.get("confidence"), verdict, lang)]
    if where_pt:
        points.append(where_pt)
    if sigs and not (cause == "sensor" and len(sigs) == 1):
        points.append(_pick([tr(lang, "it.p.sensors", list=_join([names.get(s, s) for s in sigs], lang, m)) for m in (3, 2, 1)], MAX_POINT_WORDS))
    if d.get("human_status") and len(points) < MAX_POINTS:
        points.append(tr(lang, "it.p.reviewed." + str(d["human_status"])))
    site_ref = "section:suspicious" if points_kind else (_rows_ref(place, sigs) if place else None)
    acts = [action(site, "monitor" if site_ref else "diagnoses", site_ref or d.get("id")),
            action(tr(lang, "it.a.decide"), "diagnoses", d.get("id")),
            action(tr(lang, "it.ask.diagnosis", id=d.get("id")), ask=tr(lang, "it.ask.diagnosis", id=d.get("id")))]
    level = "attention" if points_kind or cause in ("unknown", "data") or verdict == "rejected" else "problem"
    return {"kind": "diagnosis", "verdict": level, "headline": head, "points": points, "actions": acts}


def _item_flag(ws: Any, f: dict[str, Any], lang: str) -> dict[str, Any]:
    names = _signal_names(ws)
    multi = (_facts_monitor(ws)["n_groups"] or 1) > 1
    kind = str(f.get("kind") or "anomaly")
    sigs = [str(r["signal"]) for r in (f.get("signals_ranked") or []) if isinstance(r, dict) and r.get("signal")]
    sensor = _sensor(names, sigs[0], lang) if sigs else ""
    cause = str(f.get("likely_cause_class") or "unknown")
    cause = cause if cause in ("process", "sensor", "data", "mixed") else "unknown"
    if kind == "point":
        head = tr(lang, "it.fl.h.point")
    elif kind == "rule":
        head = tr(lang, "it.fl.h.rule")
    elif kind == "dq":
        problem = _dq_problem_of_flag(ws, f, lang)
        head = tr(lang, "it.fl.h.dq", problem=problem, sensor=sensor) if sensor and problem else tr(lang, "it.fl.h.dq.nosig")
    else:
        key = "it.fl.h." + (kind if kind in ("anomaly", "drift", "changepoint", "cascade") else "anomaly")
        head = tr(lang, key, sensor=sensor) if sensor else tr(lang, key + ".nosig")
    points = []
    where_pt = _where_point(f, lang, multi)
    if where_pt:
        points.append(where_pt)
    if kind != "point":
        points.append(tr(lang, "it.fl.p.cause." + cause, sure=_sure(f.get("confidence"), lang)))
    try:
        ratio = float(f.get("score")) / float(f.get("threshold"))
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = None
    if ratio is not None and ratio >= 1.2 and kind not in ("dq", "rule"):
        x = f"{ratio:.1f}" if ratio < 10 else f"{ratio:.0f}"
        points.append(tr(lang, "it.fl.p.strength", x=x if lang == "en" else x.replace(".", ",")))
    if len(sigs) > 1 and len(points) < MAX_POINTS:
        points.append(_pick([tr(lang, "it.p.sensors", list=_join([names.get(s, s) for s in sigs], lang, m)) for m in (3, 2, 1)], MAX_POINT_WORDS))
    if kind == "point":
        site = tr(lang, "it.a.site.points")
    elif cause in ("sensor", "mixed") or kind == "dq":
        site = tr(lang, "it.a.site." + ("mixed" if cause == "mixed" else "sensor"), sensor=sensor) if sensor else tr(lang, "it.a.site.sensor.nosig")
    elif cause == "data":
        site = tr(lang, "it.a.site.data")
    else:
        site = tr(lang, "it.a.site." + cause, where=_flag_where(f, lang))
    acts = [action(site, "monitor", f.get("id")), action(tr(lang, "it.a.decide"), "monitor", f.get("id")), action(tr(lang, "it.ask.flag", id=f.get("id")), ask=tr(lang, "it.ask.flag", id=f.get("id")))]
    level = "problem" if kind in _SUSTAINED and float(f.get("severity") or 0) >= 0.7 else "attention"
    return {"kind": "flag", "verdict": level, "headline": head, "points": points, "actions": acts}


def _dq_problem_of_flag(ws: Any, f: dict[str, Any], lang: str) -> str:
    text = (str(f.get("statement") or "") + " " + str(f.get("detector") or "")).lower()
    for key, needles in (("stuck", ("frozen", "stuck")), ("missing", ("missing",)), ("dropout", ("dropout", "dropped out")), ("unit_shift", ("unit", "scale")), ("out_of_range", ("range",)), ("saturation", ("saturat", "limit")), ("gap", ("gap",)), ("duplicate", ("duplicate",))):
        if any(n in text for n in needles):
            return tr(lang, "pw." + key)
    return tr(lang, "pw.other")


def _item_check(ws: Any, c: dict[str, Any], lang: str) -> dict[str, Any]:
    names = _signal_names(ws)
    status = str(c.get("status") or "")
    key = problem_key(c.get("check_type"))
    sigs = [str(s) for s in (c.get("signals") or [])]
    sensor = _sensor(names, sigs[0], lang) if sigs else ""
    cid = c.get("check_id") or c.get("id")
    if status == "pass" or key is None:
        head, use, level = tr(lang, "it.ck.h.pass"), tr(lang, "it.ck.p.use.pass"), "ok"
    else:
        problem = tr(lang, "pw." + key)
        head = tr(lang, "it.ck.h", problem=problem, sensor=sensor) if sensor else tr(lang, "it.ck.h.nosig", problem=problem)
        trust = next((t for t in (_read(ws, "trust.jsonl", []) or []) if isinstance(t, dict) and t.get("batch_id") == c.get("batch_id")), None)
        if status == "warn":
            use, level = tr(lang, "it.ck.p.use.warn"), "attention"
        elif trust is not None and not trust.get("trusted"):
            use, level = tr(lang, "it.ck.p.use.fail.batch"), "problem"
        else:
            use, level = tr(lang, "it.ck.p.use.fail.sig" if sigs else "it.ck.p.use.fail"), "attention"
    points = [p for p in (_where_point(c, lang, False, batch=True), use) if p]
    acts = []
    ref = _rows_ref(c, sigs)
    if ref and level != "ok":
        acts.append(action(tr(lang, "it.a.rows"), "monitor", ref))
    if sensor and status == "fail" and key in ("stuck", "stale", "dropout", "saturation", "unit_shift", "out_of_range", "missing", "relation_break", "impossible_value", "sign_violation"):
        acts.append(action(tr(lang, "it.ck.a.sensor", sensor=sensor), "understanding", sigs[0]))
    if len(acts) < 2:
        acts.append(action(tr(lang, "it.a.open.check"), "quality", cid))
    acts.append(action(tr(lang, "it.ask.check", id=cid), ask=tr(lang, "it.ask.check", id=cid)))
    return {"kind": "check", "verdict": level, "headline": head, "points": points, "actions": acts}


def _item_batch(ws: Any, tv: dict[str, Any], lang: str) -> dict[str, Any]:
    names = _signal_names(ws)
    bid = tv.get("batch_id")
    issue = bool(tv.get("reasons") or tv.get("local_untrusted") or tv.get("untrusted_signals"))
    level = "bad" if not tv.get("trusted") else "caution" if issue else "ok"
    kinds: Counter = Counter()
    sens: Counter = Counter()
    for l in tv.get("local_untrusted") or []:
        if isinstance(l, dict):
            k = problem_key(l.get("check_type"))
            if k:
                kinds[k] += 1
            if l.get("signal"):
                sens[str(l["signal"])] += 1
    for s in tv.get("untrusted_signals") or []:
        sens[str(s)] += 2
    points = []
    kind_list = [k for k, _ in kinds.most_common(3)]
    if level != "ok":
        for k in _facts_quality(ws)["by_batch"].get(str(bid), []):
            if k not in kind_list and len(kind_list) < 3:
                kind_list.append(k)
    if kind_list:
        points.append(_pick([tr(lang, "it.b.p.problems", list=_join([tr(lang, "pw." + k) for k in kind_list[:m]], lang, m)) for m in (3, 2, 1)], MAX_POINT_WORDS))
    elif level == "ok":
        points.append(tr(lang, "it.b.p.none"))
    if sens:
        points.append(_pick([tr(lang, "it.b.p.sensors", list=_join([names.get(s, s) for s, _ in sens.most_common(m)], lang, m)) for m in (3, 2, 1)], MAX_POINT_WORDS))
    if tv.get("trust_score") is not None:
        points.append(tr(lang, "it.b.p.score", pct=_pct(tv.get("trust_score"))))
    acts = [action(tr(lang, "it.a.open.batch"), "quality", bid), action(tr(lang, "it.ask.batch", id=bid), ask=tr(lang, "it.ask.batch", id=bid))]
    return {"kind": "batch", "verdict": {"ok": "ok", "caution": "attention", "bad": "problem"}[level], "headline": tr(lang, "it.b.h." + level, id=bid), "points": points, "actions": acts}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ0-9\"'(])", text.strip()) if s.strip()]


def _item_generic(ws: Any, item: dict[str, Any], lang: str) -> dict[str, Any]:
    """Evidence, conclusions, rules, patterns, model calls: the plain sentence other modules already wrote, with the
    method vocabulary replaced and the internal sensor names expanded (English text in every language)."""
    names = _signal_names(ws)
    text = soften(item.get("plain") or item.get("statement") or "")
    text = re.sub(r"\bS\d{2,3}\b", lambda m: names.get(m.group(0), m.group(0)) if names.get(m.group(0), m.group(0)) != m.group(0) else m.group(0), text)
    sents = [x for x in _sentences(text) if not x.startswith("Reasoning:")]
    kind = str(item.get("ref_type") or item.get("kind") or "evidence")
    head = sents[0] if sents else tr(lang, "it.ev.h")
    points = sents[1:1 + MAX_POINTS]
    if kind == "evidence" and _words(head) > MAX_HEADLINE_WORDS:
        points = [head] + points
        head = tr(lang, "it.ev.h")
    op = item.get("open") if isinstance(item.get("open"), dict) else {}
    iid = item.get("id")
    acts = []
    if op.get("view"):
        params = op.get("params") or {}
        ref = next((str(v) for v in params.values() if v), None)
        acts.append(action(tr(lang, "it.a.open.generic"), op["view"], ref))
    acts.append(action(tr(lang, "it.ask.generic", id=iid), ask=tr(lang, "it.ask.generic", id=iid)))
    return {"kind": kind, "verdict": "ok", "headline": head, "points": points, "actions": acts}


def brief_item(ws: Any, settings: Any, object_id: str, lang: str = "en") -> Optional[dict[str, Any]]:
    """{"id","kind","verdict","headline","points","actions"} for DIAG- / FLAG- / CHK- / EV- / INF- / RULE- / PATTERN- /
    EGR- ids and batch ids; None when the run does not know the id."""
    from .evidence_plain import _canonical, resolve_ref

    lang = _lang(lang)
    cid = _canonical(object_id)
    prefix = cid.split("-", 1)[0] if "-" in cid else ("B" if re.fullmatch(r"B\d{4,6}", cid) else "")
    body: Optional[dict[str, Any]] = None
    obj: Optional[dict[str, Any]] = None
    if prefix == "DIAG":
        obj = next((x for x in (_read(ws, "diagnoses.jsonl", []) or []) if isinstance(x, dict) and x.get("id") == cid), None)
        body = _item_diagnosis(ws, obj, lang) if obj else None
    elif prefix == "FLAG":
        obj = next((x for x in (_read(ws, "flags.jsonl", []) or []) if isinstance(x, dict) and x.get("id") == cid), None)
        body = _item_flag(ws, obj, lang) if obj else None
    elif prefix == "CHK":
        obj = next((x for x in (_read(ws, "checks.jsonl", []) or []) if isinstance(x, dict) and x.get("check_id") == cid), None)
        body = _item_check(ws, obj, lang) if obj else None
    elif prefix == "B":
        obj = next((x for x in (_read(ws, "trust.jsonl", []) or []) if isinstance(x, dict) and x.get("batch_id") == cid), None)
        body = _item_batch(ws, obj, lang) if obj else None
    else:
        item = resolve_ref(ws, object_id, lang=lang)
        body = _item_generic(ws, item, lang) if item else None
    if body is None:
        return None
    out = _finish(body)
    res = {"id": cid, "kind": body.get("kind"), "language": lang, "verdict": out["verdict"], "headline": out["headline"], "points": out["points"], "actions": out["actions"], "source": "template"}
    # the answer to "why is this a problem and what do I do" (round 5): from the suggestion library, so every item
    # summary in the UI carries it; a passed check needs none
    if obj is not None and body.get("kind") in ("diagnosis", "flag", "check", "batch"):
        try:
            from .advice import advice_for_object

            if not (body.get("kind") == "check" and out["verdict"] == "ok"):
                adv = advice_for_object(ws, body["kind"], obj, lang)
                res.update({"why": adv["why"], "fix": adv["fix"], "can_use_rows": adv["can_use_rows"]})
        except Exception:
            pass
    return res
