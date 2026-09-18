# Example operating rules (plain language)

Each non-empty line that does not start with `#` is one rule. Signals are referenced by alias (S01, S02, ...)
as shown in the signal catalog. Rules are compiled into executable checks and must be approved before they run.

S03 must stay between 100 and 140.
S07 must not change by more than 5 per sample.
S02 acceleration must not exceed 3 units per sample squared.
S05 must not be missing for more than 10 consecutive samples.
If S09 is above 90 then S01 must be above 45.
S12 must not stay constant for more than 30 samples.
The rolling 60-sample standard deviation of S04 must be below 8.
S10 must not drift by more than 15 over any 200-sample window.
