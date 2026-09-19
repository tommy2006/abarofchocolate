"""Suggestion library: for every kind of problem the app can show, WHY it is a problem and WHAT TO DO about it.

Plain language, English / Finnish / Swedish, deterministic templates (never a model call). One entry is::

    {"why": one sentence, "fix": [2-4 imperative steps], "use": "yes" | "no" | "partly"}   # can the rows be used?

Keys of ``LIBRARY[lang]``:
  * ``check.<check_type>``            every data-quality check type (CHECK_TYPES; rule violations are ``check.rule``)
  * ``diagnosis.<cause>.<flag kind>`` every cause class (CAUSES) x flag kind (FLAG_KINDS); flags use the same entries
  * ``batch.untrusted | caution | ok``

``advice_for(kind, subtype, facts, lang)`` fills the placeholders {sensor} {sensors} {rows} {where} {batch} {n} from
``facts``; a missing fact becomes an everyday word ("the sensor", "these rows"). ``advice_for_object`` derives subtype
and facts from a check / flag / diagnosis / trust record of a run.
"""
from __future__ import annotations

import re
from typing import Any, Optional

LANGS = ("en", "fi", "sv")
CHECK_TYPES = ("stuck", "stale", "missing", "dropout", "out_of_range", "impossible_value", "unit_shift", "duplicate_rows", "duplicate_key",
               "duplicate_timestamp", "gap", "out_of_order", "irregular_sampling", "saturation", "quantization_change", "sign_violation",
               "relation_break", "redundancy_violation", "empty_rows", "local_spike", "frozen_block", "missing_block", "quantization_block",
               "plausibility", "timeliness_not_testable", "rule", "other")
CAUSES = ("process", "sensor", "data", "mixed", "unknown")
FLAG_KINDS = ("anomaly", "drift", "changepoint", "point", "cascade", "dq", "rule")
BATCH_KINDS = ("untrusted", "caution", "ok")
USE = ("yes", "no", "partly")
MAX_FIX = 4

DEFAULTS = {
    "en": {"sensor": "the sensor", "sensors": "the sensors involved", "rows": "these rows", "where": "this place", "batch": "this batch", "n": "several"},
    "fi": {"sensor": "anturi", "sensors": "osalliset anturit", "rows": "nämä rivit", "where": "tämä kohta", "batch": "tämä erä", "n": "usea"},
    "sv": {"sensor": "givaren", "sensors": "de berörda givarna", "rows": "de här raderna", "where": "det här stället", "batch": "den här satsen", "n": "flera"},
}

# ------------------------------------------------------------------------------------------------ data-quality checks
_CHECKS: dict[str, dict[str, tuple[str, list[str], str]]] = {
    "en": {
        "stuck": ("{sensor} did not change at all in {rows}, which a live measurement never does: the sensor, its wiring or the data link most likely froze.",
                  ["On site, check that {sensor} has power, is connected and is not blocked or iced up.", "Compare its value with a nearby gauge; if they differ, have it recalibrated or replaced.",
                   "Leave {rows} of {sensor} out of any analysis; the other sensors can still be used.", "If the value really was steady (valve closed, tank empty), accept the finding with a note."], "partly"),
        "stale": ("{sensor} stopped updating for a long time in {rows} although it normally refreshes regularly: the instrument or its link to the logger paused.",
                  ["Check the instrument and its data link; a lab analyser may have missed a sample cycle.", "Check the logger for a paused or delayed collection.", "Treat the value as unknown for {rows}; the rest of {batch} can be used."], "partly"),
        "missing": ("Values of {sensor} are absent in {rows}: the sensor sent nothing, the logger dropped them, or the export left them out.",
                    ["Check whether the plant database still has these values and export them again if it does.", "If the sensor really sent nothing, check its power, wiring and network link.", "Do not fill the holes with guesses; leave {rows} out for {sensor} and use the other sensors."], "partly"),
        "dropout": ("{sensor} has no values at all in {batch}: it was disconnected, switched off, or missing from the file for this stretch.",
                    ["Check the instrument and its connection to the control system for that time.", "Check whether the column was left out of the export.", "Use the other sensors of {batch}; nothing can be said about {sensor} here."], "partly"),
        "out_of_range": ("{sensor} shows values far outside its usual range in {rows}: usually a transmitter fault, a wiring problem or a wrong scale, more rarely a real extreme.",
                         ["Check the transmitter and its wiring; a loose connection often produces extreme values.", "Ask the operators whether anything unusual happened then; a real extreme shows on related sensors too.", "Set {rows} of {sensor} aside until the cause is known; the other sensors can be used."], "partly"),
        "impossible_value": ("{sensor} contains values that cannot physically occur in {rows}: a calculation error, a corrupt record or a transmission fault.",
                             ["Check the export or the logger calculation that produced these values.", "Remove {rows} of {sensor} or mark them invalid before using the data.", "If it happens again, check the transmitter and the data link."], "partly"),
        "unit_shift": ("{sensor} suddenly changed scale by a factor of ten or more in {rows}: it looks like a different unit or a moved decimal point, not a real change in the process.",
                       ["Check whether the unit or scaling of {sensor} was changed in the control system, the logger or the export.", "Convert the affected values back if the factor is known; otherwise set them aside.", "Tell the system the correct unit by naming the sensor on the Understanding page."], "partly"),
        "duplicate_rows": ("Some rows of {batch} are exact copies of others: the export or the logger wrote the same record more than once.",
                           ["Remove the duplicate rows before using the data; keep the first copy.", "Check the export or the collection job for a repeated read.", "The other rows of {batch} can be used as they are."], "partly"),
        "frozen_block": ("{sensors} stayed frozen in the same rows ({rows}): when several signals stop at the same moment, the logger, the export or the data link froze, not the sensors.",
                  ["Check the logger or the export for a pause or a repeated read at that time.", "Get the values again from the plant database if it still has them.", "Leave {rows} out for these signals; do not blame the individual sensors.", "If the process really stood still (a shutdown), accept the finding with a note."], "partly"),
        "missing_block": ("{sensors} are missing in the same rows ({rows}): a gap in the collection or the export, not separate sensor faults.",
                  ["Check the logger and the network for an outage at that time.", "Export the stretch again from the plant database if it still has the values.", "Leave {rows} out; do not fill the gap with guesses."], "partly"),
        "quantization_block": ("{sensors} changed resolution at the same moment ({rows}): a setting in the logger or the export changed, not the instruments.",
                  ["Check whether the logger or export settings (rounding, compression, deadband) changed at that time.", "Restore the earlier setting if it was changed by mistake.", "Treat small changes of these signals after that moment with caution."], "partly"),
        "plausibility": ("{sensor} shows values outside the physically plausible range in {rows} (for example a negative flow or a share above 100 %): a scaling, wiring or calculation error.",
                  ["Check the transmitter scaling and the unit in the control system.", "Check the wiring and the logger calculation that produce this value.", "Set {rows} of {sensor} aside until the cause is fixed; the other sensors can be used."], "partly"),
        "timeliness_not_testable": ("Whether the data arrived on time could not be tested: the file has no usable time column, so delays and gaps in time cannot be seen.",
                  ["Include the time stamp column in the export if the source has one.", "The other checks ran as usual; their results can be used."], "yes"),
        "duplicate_key": ("Some rows of {batch} reuse an identification that already exists, so it is unclear which record is the right one.",
                          ["Check the export for a merge of two sources or a repeated run.", "Keep one record per identification, then run the analysis again.", "Until then, treat these rows with caution."], "partly"),
        "duplicate_timestamp": ("Several rows of {batch} carry the same time stamp: more than one record was written for the same moment.",
                                ["Check the logging rate and whether two sources were merged.", "Keep one record per time stamp and run the analysis again.", "The values are probably fine; only the timing is unclear."], "partly"),
        "gap": ("Time jumps in {batch}: rows are missing for much longer than the usual interval, so nothing is known about that period.",
                ["Check whether the logger stopped collecting around {rows}: a restart or a network outage.", "Export the missing period again if the plant database still has it.", "Draw no conclusions about the process during the gap; the rows around it can be used."], "partly"),
        "out_of_order": ("Time stamps go backwards in {batch}: the rows are not in the order they were recorded, typically after files were merged or a clock was changed.",
                         ["Sort the file by time stamp before uploading it again.", "Check the logger for a clock change or a time-zone mix-up.", "Until the order is fixed, findings that depend on the sequence of events are unreliable."], "partly"),
        "irregular_sampling": ("The time between readings in {batch} varies a lot: the logger skipped or delayed readings.",
                               ["Check the logger load and the collection interval.", "Export at a fixed interval if your plant database supports it.", "The values can be used; only conclusions about timing are less reliable."], "yes"),
        "saturation": ("{sensor} sits exactly at its upper or lower limit in {rows}: the instrument cannot measure beyond its range, so the real value is unknown there.",
                       ["Check that the measuring range of {sensor} is set correctly in the transmitter.", "If the process really reached that limit, consider an instrument with a wider range.", "Read the flat stretch as \"at least / at most this value\", not as a measurement."], "partly"),
        "quantization_change": ("{sensor} is recorded in much coarser steps than usual in {rows}: the logger, the transmitter or the export changed its precision.",
                                ["Check the compression setting of the logger and the precision of the export.", "Restore the usual precision at the source if you can.", "The values can be used, but small changes are invisible in this stretch."], "yes"),
        "sign_violation": ("{sensor} shows negative values in {rows} although it is never negative elsewhere: a sign error in the scaling, swapped wires or a wrong zero point.",
                           ["Check the zero point and the scaling of the transmitter.", "Check whether the wiring or a sign in a calculation was reversed.", "Set {rows} of {sensor} aside until the cause is known."], "partly"),
        "relation_break": ("{sensor} stopped moving together with the sensors it normally follows, while those still agree with each other: the odd one out is probably the faulty instrument.",
                           ["Compare {sensor} with its partner sensors on site or against a portable reference.", "Check calibration, fouling and mounting of {sensor}.", "Prefer the partner sensors for {rows} until {sensor} has been checked."], "partly"),
        "redundancy_violation": ("{sensor} is normally a copy or a sum of other sensors, but in {rows} it no longer matches them: the calculation or one of its inputs changed.",
                                 ["Check the formula in the control system that produces {sensor}.", "Check the input sensors of that formula for their own problems.", "Use the original sensors rather than the calculated one for {rows}."], "partly"),
        "empty_rows": ("Some rows of {batch} contain no values at all: the logger wrote the time but no measurements.",
                       ["Check the export and the logger for a collection pause.", "Drop the empty rows before using the data.", "The rest of {batch} can be used."], "partly"),
        "local_spike": ("{sensor} jumps away from its neighbours for a single reading in {rows} and comes straight back: a glitch or a manipulated value; the data alone cannot tell which.",
                        ["Compare the moment with the operators' notes and the event list of the control system.", "If such glitches repeat on {sensor}, check its wiring and shielding.", "Leave the single reading out; the rows around it can be used."], "partly"),
        "rule": ("One of your own operating rules was broken in {rows}: the data does not meet a condition you said must always hold.",
                 ["Read the rule and check on site whether the condition is really broken.", "If the rule is wrong or too strict, correct or retire it on the Data quality page.", "If it is right, act on it as a process condition, not as a data problem."], "yes"),
        "other": ("A data check found something unusual in {rows}; the statement of the check gives the details.",
                  ["Read the statement of the check and look at the rows on the timeline.", "Set the affected rows aside if you are unsure.", "Ask the system about this check for more detail."], "partly"),
    },
    "fi": {
        "stuck": ("{sensor} ei muuttunut lainkaan ({rows}), mitä oikea mittaus ei koskaan tee: anturi, sen johdotus tai tiedonsiirto on todennäköisesti jumiutunut.",
                  ["Tarkista paikan päällä, että {sensor} saa virtaa, on kytketty eikä ole tukossa tai jäässä.", "Vertaa arvoa lähellä olevaan mittariin; jos ne eroavat, kalibroi tai vaihda mittalaite.",
                   "Jätä nämä rivit ({rows}) pois tämän anturin osalta; muita antureita voi käyttää.", "Jos arvo oli oikeasti vakaa (venttiili kiinni, säiliö tyhjä), hyväksy löydös ja kirjoita huomautus."], "partly"),
        "stale": ("{sensor} lakkasi päivittymästä pitkäksi aikaa ({rows}), vaikka se yleensä päivittyy säännöllisesti: mittalaite tai sen yhteys tallentimeen pysähtyi.",
                  ["Tarkista mittalaite ja sen tiedonsiirto; laboratorioanalysaattorilta on voinut jäädä näytekierros väliin.", "Tarkista, onko tallentimen keruu pysähtynyt tai viivästynyt.", "Pidä arvoa tuntemattomana ({rows}); muuta erää {batch} voi käyttää."], "partly"),
        "missing": ("Anturin {sensor} arvoja puuttuu ({rows}): anturi ei lähettänyt mitään, tallennin pudotti ne tai vienti jätti ne pois.",
                    ["Tarkista, ovatko arvot vielä laitoksen tietokannassa, ja vie ne uudelleen, jos ovat.", "Jos anturi ei oikeasti lähettänyt mitään, tarkista sen virta, johdotus ja verkkoyhteys.", "Älä täytä aukkoja arvauksilla; jätä rivit ({rows}) pois tämän anturin osalta ja käytä muita antureita."], "partly"),
        "dropout": ("Anturilla {sensor} ei ole lainkaan arvoja erässä {batch}: se oli irti, pois päältä tai puuttui tiedostosta tältä jaksolta.",
                    ["Tarkista mittalaite ja sen yhteys ohjausjärjestelmään kyseiseltä ajalta.", "Tarkista, jäikö sarake pois viennistä.", "Käytä erän {batch} muita antureita; tästä anturista ei voi sanoa mitään."], "partly"),
        "out_of_range": ("{sensor} näyttää arvoja kaukana tavallisen alueensa ulkopuolella ({rows}): yleensä lähetinvika, johdotusongelma tai väärä asteikko, harvemmin todellinen ääriarvo.",
                         ["Tarkista lähetin ja sen johdotus; löysä liitos tuottaa usein ääriarvoja.", "Kysy operaattoreilta, tapahtuiko silloin jotain poikkeavaa; todellinen ääriarvo näkyy myös muissa antureissa.", "Siirrä rivit ({rows}) sivuun tämän anturin osalta, kunnes syy tiedetään; muita antureita voi käyttää."], "partly"),
        "impossible_value": ("Anturissa {sensor} on arvoja, jotka eivät ole fysikaalisesti mahdollisia ({rows}): laskentavirhe, vioittunut tietue tai tiedonsiirtovika.",
                             ["Tarkista vienti tai tallentimen laskenta, joka tuotti nämä arvot.", "Poista rivit ({rows}) tai merkitse ne virheellisiksi ennen datan käyttöä.", "Jos vika toistuu, tarkista lähetin ja tiedonsiirto."], "partly"),
        "unit_shift": ("Anturin {sensor} asteikko muuttui äkkiä vähintään kymmenkertaisesti ({rows}): se näyttää eri yksiköltä tai siirtyneeltä desimaalipilkulta, ei todelliselta muutokselta prosessissa.",
                       ["Tarkista, muutettiinko anturin yksikköä tai skaalausta ohjausjärjestelmässä, tallentimessa tai viennissä.", "Muunna arvot takaisin, jos kerroin tiedetään; muuten siirrä ne sivuun.", "Kerro järjestelmälle oikea yksikkö nimeämällä anturi Ymmärrys-sivulla."], "partly"),
        "duplicate_rows": ("Osa erän {batch} riveistä on toisten rivien täsmällisiä kopioita: vienti tai tallennin kirjoitti saman tietueen useammin kuin kerran.",
                           ["Poista kaksoisrivit ennen datan käyttöä; säilytä ensimmäinen.", "Tarkista, lukeeko vienti tai keruuajo saman tiedon kahdesti.", "Erän {batch} muita rivejä voi käyttää sellaisenaan."], "partly"),
        "frozen_block": ("{sensors} pysyivät jumissa samoilla riveillä ({rows}): kun useat signaalit pysähtyvät samalla hetkellä, jumissa oli tallennin, vienti tai tiedonsiirto eivätkä anturit.",
                  ["Tarkista tallentimesta tai viennistä, oliko silloin tauko tai toistuva luku.", "Hae arvot uudelleen laitoksen tietokannasta, jos ne ovat vielä siellä.", "Jätä nämä rivit pois näiden signaalien osalta; älä syytä yksittäisiä antureita.", "Jos prosessi todella seisoi (alasajo), hyväksy havainto ja kirjaa huomautus."], "partly"),
        "missing_block": ("{sensors} puuttuvat samoilta riveiltä ({rows}): aukko keruussa tai viennissä, ei erillisiä anturivikoja.",
                  ["Tarkista tallentimesta ja verkosta, oliko silloin katko.", "Vie jakso uudelleen laitoksen tietokannasta, jos arvot ovat vielä siellä.", "Jätä nämä rivit pois; älä täytä aukkoa arvauksilla."], "partly"),
        "quantization_block": ("{sensors} muuttivat tarkkuuttaan samalla hetkellä ({rows}): tallentimen tai viennin asetus muuttui, eivät mittalaitteet.",
                  ["Tarkista, muuttuivatko tallentimen tai viennin asetukset (pyöristys, pakkaus, kuollut alue) silloin.", "Palauta aiempi asetus, jos se muutettiin vahingossa.", "Suhtaudu varauksella näiden signaalien pieniin muutoksiin tuon hetken jälkeen."], "partly"),
        "plausibility": ("{sensor} näyttää fyysisesti epäuskottavia arvoja ({rows}), esimerkiksi negatiivisen virtauksen tai yli 100 %:n osuuden: skaalaus-, johdotus- tai laskentavirhe.",
                  ["Tarkista lähettimen skaalaus ja yksikkö ohjausjärjestelmässä.", "Tarkista johdotus ja tallentimen laskenta, joka tuottaa arvon.", "Jätä nämä rivit sivuun tämän anturin osalta, kunnes syy on korjattu; muita antureita voi käyttää."], "partly"),
        "timeliness_not_testable": ("Datan ajantasaisuutta ei voitu testata: tiedostossa ei ole käyttökelpoista aikasaraketta, joten viiveitä ja aukkoja ajassa ei näe.",
                  ["Ota aikaleimasarake mukaan vientiin, jos lähteessä on sellainen.", "Muut tarkistukset ajettiin normaalisti; niiden tuloksia voi käyttää."], "yes"),
        "duplicate_key": ("Osa erän {batch} riveistä käyttää tunnistetta, joka on jo olemassa, joten ei ole selvää, mikä tietue on oikea.",
                          ["Tarkista, onko viennissä yhdistetty kaksi lähdettä tai toistettu ajo.", "Säilytä yksi tietue tunnistetta kohti ja aja analyysi uudelleen.", "Käsittele näitä rivejä siihen asti varoen."], "partly"),
        "duplicate_timestamp": ("Usealla erän {batch} rivillä on sama aikaleima: samalle hetkelle kirjoitettiin useampi tietue.",
                                ["Tarkista tallennustiheys ja onko kaksi lähdettä yhdistetty.", "Säilytä yksi tietue aikaleimaa kohti ja aja analyysi uudelleen.", "Arvot ovat todennäköisesti kunnossa; vain ajoitus on epäselvä."], "partly"),
        "gap": ("Aika hyppää erässä {batch}: rivejä puuttuu paljon tavallista väliä pidemmältä ajalta, joten siitä jaksosta ei tiedetä mitään.",
                ["Tarkista, pysähtyikö tallentimen keruu ({rows}): uudelleenkäynnistys tai verkkokatko.", "Vie puuttuva jakso uudelleen, jos se on vielä laitoksen tietokannassa.", "Älä tee päätelmiä prosessista aukon ajalta; sen ympärillä olevia rivejä voi käyttää."], "partly"),
        "out_of_order": ("Aikaleimat kulkevat taaksepäin erässä {batch}: rivit eivät ole tallennusjärjestyksessä, tyypillisesti tiedostojen yhdistämisen tai kellon muutoksen jälkeen.",
                         ["Järjestä tiedosto aikaleiman mukaan ennen kuin lataat sen uudelleen.", "Tarkista tallentimen kellonmuutos tai aikavyöhykesekaannus.", "Ennen korjausta tapahtumien järjestykseen perustuvat löydökset ovat epäluotettavia."], "partly"),
        "irregular_sampling": ("Lukemien väli vaihtelee paljon erässä {batch}: tallennin ohitti tai viivästytti lukemia.",
                               ["Tarkista tallentimen kuorma ja keruuväli.", "Vie data kiinteällä välillä, jos laitoksen tietokanta tukee sitä.", "Arvoja voi käyttää; vain ajoitusta koskevat päätelmät ovat epävarmempia."], "yes"),
        "saturation": ("{sensor} pysyy täsmälleen ylä- tai alarajallaan ({rows}): mittalaite ei pysty mittaamaan alueensa ulkopuolelta, joten todellinen arvo ei ole tiedossa.",
                       ["Tarkista, että anturin mittausalue on asetettu lähettimessä oikein.", "Jos prosessi todella saavutti rajan, harkitse laajemman alueen mittalaitetta.", "Lue tasainen jakso muodossa \"vähintään / enintään tämä arvo\", ei mittauksena."], "partly"),
        "quantization_change": ("{sensor} on tallennettu paljon tavallista karkeammin portain ({rows}): tallennin, lähetin tai vienti muutti tarkkuuttaan.",
                                ["Tarkista tallentimen pakkausasetus ja viennin tarkkuus.", "Palauta tavallinen tarkkuus lähteessä, jos voit.", "Arvoja voi käyttää, mutta pienet muutokset eivät näy tällä jaksolla."], "yes"),
        "sign_violation": ("{sensor} näyttää negatiivisia arvoja ({rows}), vaikka se ei muualla ole koskaan negatiivinen: merkkivirhe skaalauksessa, ristiin kytketyt johdot tai väärä nollapiste.",
                           ["Tarkista lähettimen nollapiste ja skaalaus.", "Tarkista, onko johdotus tai laskennan etumerkki käännetty.", "Siirrä rivit ({rows}) sivuun tämän anturin osalta, kunnes syy tiedetään."], "partly"),
        "relation_break": ("{sensor} lakkasi liikkumasta yhdessä niiden antureiden kanssa, joita se yleensä seuraa, vaikka ne ovat yhä keskenään samaa mieltä: poikkeava on todennäköisesti viallinen mittalaite.",
                           ["Vertaa anturia paikan päällä sen pariantureihin tai kannettavaan vertailumittariin.", "Tarkista anturin kalibrointi, likaantuminen ja kiinnitys.", "Käytä pariantureita ({rows}), kunnes {sensor} on tarkistettu."], "partly"),
        "redundancy_violation": ("{sensor} on yleensä muiden antureiden kopio tai summa, mutta nyt ({rows}) se ei enää vastaa niitä: laskenta tai jokin sen syötteistä muuttui.",
                                 ["Tarkista ohjausjärjestelmän kaava, joka tuottaa tämän arvon.", "Tarkista kaavan syöteanturit niiden omien ongelmien varalta.", "Käytä alkuperäisiä antureita lasketun arvon sijaan ({rows})."], "partly"),
        "empty_rows": ("Osa erän {batch} riveistä on täysin tyhjiä: tallennin kirjoitti ajan mutta ei mittauksia.",
                       ["Tarkista, onko viennissä tai tallentimessa ollut keruutauko.", "Poista tyhjät rivit ennen datan käyttöä.", "Muuta erää {batch} voi käyttää."], "partly"),
        "local_spike": ("{sensor} hyppää yhden lukeman ajaksi pois naapureistaan ({rows}) ja palaa heti: häiriö tai manipuloitu arvo; pelkästä datasta sitä ei voi päätellä.",
                        ["Vertaa hetkeä operaattoreiden muistiinpanoihin ja ohjausjärjestelmän tapahtumalistaan.", "Jos häiriöt toistuvat tässä anturissa, tarkista sen johdotus ja suojaus.", "Jätä yksittäinen lukema pois; sen ympärillä olevia rivejä voi käyttää."], "partly"),
        "rule": ("Yksi omista käyttösäännöistäsi rikkoutui ({rows}): data ei täytä ehtoa, jonka sanoit aina pätevän.",
                 ["Lue sääntö ja tarkista paikan päällä, onko ehto todella rikki.", "Jos sääntö on väärä tai liian tiukka, korjaa tai poista se Datan laatu -sivulla.", "Jos se on oikea, toimi sen mukaan prosessin tilana, ei dataongelmana."], "yes"),
        "other": ("Datan tarkistus löysi jotain poikkeavaa ({rows}); tarkistuksen oma kuvaus kertoo yksityiskohdat.",
                  ["Lue tarkistuksen kuvaus ja katso rivit aikajanalta.", "Siirrä rivit sivuun, jos olet epävarma.", "Kysy järjestelmältä lisää tästä tarkistuksesta."], "partly"),
    },
    "sv": {
        "stuck": ("{sensor} ändrades inte alls ({rows}), vilket en riktig mätning aldrig gör: givaren, dess kablar eller dataöverföringen har troligen hängt sig.",
                  ["Kontrollera på plats att {sensor} har ström, är ansluten och inte är igensatt eller isbelagd.", "Jämför värdet med en mätare i närheten; om de skiljer sig, kalibrera eller byt instrumentet.",
                   "Lämna raderna ({rows}) utanför analysen för den här givaren; de andra givarna kan användas.", "Om värdet verkligen var stabilt (stängd ventil, tom tank), godkänn fyndet med en anteckning."], "partly"),
        "stale": ("{sensor} slutade uppdateras under lång tid ({rows}) fast den normalt uppdateras regelbundet: instrumentet eller dess länk till loggern stannade.",
                  ["Kontrollera instrumentet och dess datalänk; en labbanalysator kan ha missat en provcykel.", "Kontrollera om loggerns insamling har pausats eller försenats.", "Betrakta värdet som okänt ({rows}); resten av {batch} kan användas."], "partly"),
        "missing": ("Värden från {sensor} saknas ({rows}): givaren skickade inget, loggern tappade dem eller exporten utelämnade dem.",
                    ["Kontrollera om värdena finns kvar i anläggningens databas och exportera dem igen i så fall.", "Om givaren verkligen inte skickade något, kontrollera ström, kablar och nätverk.", "Fyll inte hålen med gissningar; lämna raderna ({rows}) utanför för den här givaren och använd de andra."], "partly"),
        "dropout": ("{sensor} har inga värden alls i {batch}: den var frånkopplad, avstängd eller saknades i filen under den här perioden.",
                    ["Kontrollera instrumentet och dess anslutning till styrsystemet för den tiden.", "Kontrollera om kolumnen utelämnades i exporten.", "Använd de andra givarna i {batch}; om den här givaren går inget att säga."], "partly"),
        "out_of_range": ("{sensor} visar värden långt utanför sitt vanliga område ({rows}): oftast ett fel i mätomvandlaren, ett kabelproblem eller fel skala, mer sällan ett verkligt extremvärde.",
                         ["Kontrollera mätomvandlaren och dess kablar; en lös anslutning ger ofta extremvärden.", "Fråga operatörerna om något ovanligt hände då; ett verkligt extremvärde syns också på andra givare.", "Lägg raderna ({rows}) åt sidan för den här givaren tills orsaken är känd; de andra givarna kan användas."], "partly"),
        "impossible_value": ("{sensor} innehåller värden som inte är fysiskt möjliga ({rows}): ett räknefel, en skadad post eller ett överföringsfel.",
                             ["Kontrollera exporten eller loggerns beräkning som gav dessa värden.", "Ta bort raderna ({rows}) eller markera dem som ogiltiga innan data används.", "Om det upprepas, kontrollera mätomvandlaren och datalänken."], "partly"),
        "unit_shift": ("{sensor} bytte plötsligt skala med minst en faktor tio ({rows}): det ser ut som en annan enhet eller ett flyttat decimaltecken, inte som en verklig förändring i processen.",
                       ["Kontrollera om givarens enhet eller skalning ändrades i styrsystemet, loggern eller exporten.", "Räkna tillbaka värdena om faktorn är känd; lägg dem annars åt sidan.", "Berätta rätt enhet för systemet genom att namnge givaren på sidan Förståelse."], "partly"),
        "duplicate_rows": ("Några rader i {batch} är exakta kopior av andra: exporten eller loggern skrev samma post mer än en gång.",
                           ["Ta bort dubblettraderna innan data används; behåll den första.", "Kontrollera om exporten eller insamlingen läser samma data två gånger.", "De andra raderna i {batch} kan användas som de är."], "partly"),
        "frozen_block": ("{sensors} stod stilla i samma rader ({rows}): när flera signaler stannar i samma ögonblick är det loggern, exporten eller dataöverföringen som hängt sig, inte givarna.",
                  ["Kontrollera loggern eller exporten för ett uppehåll eller en upprepad läsning vid den tiden.", "Hämta värdena igen från anläggningens databas om de finns kvar.", "Lämna de här raderna utanför för dessa signaler; skyll inte på de enskilda givarna.", "Om processen verkligen stod still (en nedstängning), godkänn fyndet med en kommentar."], "partly"),
        "missing_block": ("{sensors} saknas i samma rader ({rows}): ett avbrott i insamlingen eller exporten, inte separata givarfel.",
                  ["Kontrollera loggern och nätverket för ett avbrott vid den tiden.", "Exportera sträckan igen från anläggningens databas om värdena finns kvar.", "Lämna de här raderna utanför; fyll inte luckan med gissningar."], "partly"),
        "quantization_block": ("{sensors} bytte upplösning i samma ögonblick ({rows}): en inställning i loggern eller exporten ändrades, inte instrumenten.",
                  ["Kontrollera om loggerns eller exportens inställningar (avrundning, komprimering, dödband) ändrades vid den tiden.", "Återställ den tidigare inställningen om den ändrades av misstag.", "Behandla små förändringar i dessa signaler efter den tidpunkten med försiktighet."], "partly"),
        "plausibility": ("{sensor} visar värden utanför det fysiskt rimliga intervallet ({rows}), till exempel ett negativt flöde eller en andel över 100 %: ett skalnings-, kabel- eller beräkningsfel.",
                  ["Kontrollera transmitterns skalning och enheten i styrsystemet.", "Kontrollera kablaget och loggerns beräkning som ger värdet.", "Lägg de här raderna för {sensor} åt sidan tills orsaken är åtgärdad; de andra givarna kan användas."], "partly"),
        "timeliness_not_testable": ("Om data kom i tid kunde inte testas: filen har ingen användbar tidskolumn, så fördröjningar och luckor i tiden syns inte.",
                  ["Ta med tidsstämpelkolumnen i exporten om källan har en.", "De andra kontrollerna kördes som vanligt; resultaten kan användas."], "yes"),
        "duplicate_key": ("Några rader i {batch} återanvänder en identitet som redan finns, så det är oklart vilken post som är rätt.",
                          ["Kontrollera om exporten slår ihop två källor eller upprepar en körning.", "Behåll en post per identitet och kör analysen igen.", "Behandla raderna med försiktighet tills dess."], "partly"),
        "duplicate_timestamp": ("Flera rader i {batch} har samma tidsstämpel: mer än en post skrevs för samma ögonblick.",
                                ["Kontrollera loggningstakten och om två källor har slagits ihop.", "Behåll en post per tidsstämpel och kör analysen igen.", "Värdena är troligen korrekta; bara tidpunkten är oklar."], "partly"),
        "gap": ("Tiden hoppar i {batch}: rader saknas under mycket längre tid än det vanliga intervallet, så inget är känt om den perioden.",
                ["Kontrollera om loggern slutade samla in ({rows}): en omstart eller ett nätverksavbrott.", "Exportera den saknade perioden igen om den finns kvar i anläggningens databas.", "Dra inga slutsatser om processen under luckan; raderna runt den kan användas."], "partly"),
        "out_of_order": ("Tidsstämplarna går bakåt i {batch}: raderna ligger inte i den ordning de registrerades, typiskt efter att filer slagits ihop eller en klocka ändrats.",
                         ["Sortera filen efter tidsstämpel innan du laddar upp den igen.", "Kontrollera om loggerns klocka ändrats eller tidszoner blandats ihop.", "Tills ordningen är rättad är fynd som bygger på händelsernas ordning opålitliga."], "partly"),
        "irregular_sampling": ("Tiden mellan avläsningarna varierar mycket i {batch}: loggern hoppade över eller försenade avläsningar.",
                               ["Kontrollera loggerns belastning och insamlingsintervall.", "Exportera med fast intervall om anläggningens databas stöder det.", "Värdena kan användas; bara slutsatser om tidpunkter är mindre säkra."], "yes"),
        "saturation": ("{sensor} ligger exakt på sin övre eller nedre gräns ({rows}): instrumentet kan inte mäta utanför sitt område, så det verkliga värdet är okänt där.",
                       ["Kontrollera att givarens mätområde är rätt inställt i mätomvandlaren.", "Om processen verkligen nådde gränsen, överväg ett instrument med större område.", "Läs den platta sträckan som \"minst / högst detta värde\", inte som en mätning."], "partly"),
        "quantization_change": ("{sensor} registreras i mycket grövre steg än vanligt ({rows}): loggern, mätomvandlaren eller exporten ändrade sin noggrannhet.",
                                ["Kontrollera loggerns komprimeringsinställning och exportens noggrannhet.", "Återställ den vanliga noggrannheten vid källan om du kan.", "Värdena kan användas, men små förändringar syns inte i den här sträckan."], "yes"),
        "sign_violation": ("{sensor} visar negativa värden ({rows}) fast den aldrig är negativ annars: ett teckenfel i skalningen, omkastade kablar eller fel nollpunkt.",
                           ["Kontrollera mätomvandlarens nollpunkt och skalning.", "Kontrollera om kablarna eller ett tecken i en beräkning har vänts.", "Lägg raderna ({rows}) åt sidan för den här givaren tills orsaken är känd."], "partly"),
        "relation_break": ("{sensor} slutade röra sig tillsammans med de givare den normalt följer, medan de fortfarande stämmer med varandra: den avvikande är troligen det felaktiga instrumentet.",
                           ["Jämför givaren på plats med dess partnergivare eller med en bärbar referens.", "Kontrollera givarens kalibrering, nedsmutsning och montering.", "Lita på partnergivarna ({rows}) tills {sensor} har kontrollerats."], "partly"),
        "redundancy_violation": ("{sensor} är normalt en kopia eller summa av andra givare, men nu ({rows}) stämmer den inte längre med dem: beräkningen eller någon av dess insignaler ändrades.",
                                 ["Kontrollera formeln i styrsystemet som ger det här värdet.", "Kontrollera formelns ingående givare för egna problem.", "Använd de ursprungliga givarna i stället för det beräknade värdet ({rows})."], "partly"),
        "empty_rows": ("Några rader i {batch} är helt tomma: loggern skrev tiden men inga mätvärden.",
                       ["Kontrollera om exporten eller loggern hade ett uppehåll i insamlingen.", "Ta bort de tomma raderna innan data används.", "Resten av {batch} kan användas."], "partly"),
        "local_spike": ("{sensor} hoppar bort från sina grannar under en enda avläsning ({rows}) och kommer genast tillbaka: en störning eller ett manipulerat värde; enbart data kan inte avgöra det.",
                        ["Jämför ögonblicket med operatörernas anteckningar och styrsystemets händelselista.", "Om störningarna upprepas på den här givaren, kontrollera kablar och skärmning.", "Lämna den enskilda avläsningen utanför; raderna runt den kan användas."], "partly"),
        "rule": ("En av dina egna driftregler bröts ({rows}): data uppfyller inte ett villkor som du sagt alltid ska gälla.",
                 ["Läs regeln och kontrollera på plats om villkoret verkligen är brutet.", "Om regeln är fel eller för sträng, rätta eller ta bort den på sidan Datakvalitet.", "Om den är rätt, agera på den som ett processtillstånd, inte som ett dataproblem."], "yes"),
        "other": ("En datakontroll hittade något ovanligt ({rows}); kontrollens egen beskrivning ger detaljerna.",
                  ["Läs kontrollens beskrivning och titta på raderna på tidslinjen.", "Lägg raderna åt sidan om du är osäker.", "Fråga systemet om mer detaljer om den här kontrollen."], "partly"),
    },
}

# ------------------------------------------------------------------------------------------------ findings: cause x kind
_KIND_WHAT = {
    "en": {"anomaly": "The readings at {where} do not match the normal operation the system learned, mainly on {sensor}.",
           "drift": "The readings at {where} moved slowly away from normal operation, mainly on {sensor}.",
           "changepoint": "The behaviour changed suddenly at {where}; {sensor} reacted first.",
           "cascade": "A disturbance spread from sensor to sensor at {where}, starting at {sensor}.",
           "point": "A single reading at {where} does not fit its surroundings.",
           "dq": "The alarm at {where} is about the data, not the process.",
           "rule": "One of your operating rules was broken at {where}."},
    "fi": {"anomaly": "Lukemat ({where}) eivät vastaa järjestelmän oppimaa normaalia toimintaa; eniten poikkeaa {sensor}.",
           "drift": "Lukemat ({where}) siirtyivät hitaasti pois normaalista toiminnasta; eniten poikkeaa {sensor}.",
           "changepoint": "Käyttäytyminen muuttui äkisti ({where}); ensimmäisenä reagoi {sensor}.",
           "cascade": "Häiriö levisi anturista toiseen ({where}); se alkoi tästä: {sensor}.",
           "point": "Yksittäinen lukema ({where}) ei sovi ympäristöönsä.",
           "dq": "Hälytys ({where}) koskee dataa, ei prosessia.",
           "rule": "Yksi käyttösäännöistäsi rikkoutui ({where})."},
    "sv": {"anomaly": "Avläsningarna ({where}) stämmer inte med den normala drift som systemet lärt sig; mest avviker {sensor}.",
           "drift": "Avläsningarna ({where}) gled långsamt bort från normal drift; mest avviker {sensor}.",
           "changepoint": "Beteendet ändrades plötsligt ({where}); först reagerade {sensor}.",
           "cascade": "En störning spred sig från givare till givare ({where}); den började här: {sensor}.",
           "point": "En enskild avläsning ({where}) passar inte in i sin omgivning.",
           "dq": "Larmet ({where}) gäller data, inte processen.",
           "rule": "En av dina driftregler bröts ({where})."},
}
_CAUSE_WHY = {
    "en": {"process": "Several related sensors moved together and kept their usual relations, so the process itself changed, not one instrument.",
           "sensor": "One sensor broke its usual relation to its partners while they stayed consistent, so the instrument is the likely cause.",
           "data": "The change looks like a data problem (scale, duplicates, gaps, frozen values), not a physical event.",
           "mixed": "Both a change in the process and an instrument problem remain plausible.",
           "unknown": "The evidence does not favour the process or a sensor; it can be a glitch, a manipulation or a real event.",
           "point": "It is a glitch or a manipulation; the data alone can't tell."},
    "fi": {"process": "Useat toisiinsa liittyvät anturit liikkuivat yhdessä ja säilyttivät tavalliset suhteensa, joten itse prosessi muuttui, ei yksittäinen mittalaite.",
           "sensor": "Yksi anturi rikkoi tavallisen suhteensa pariantureihinsa, vaikka ne pysyivät johdonmukaisina, joten mittalaite on todennäköinen syy.",
           "data": "Muutos näyttää dataongelmalta (asteikko, kaksoisrivit, aukot, jumiutuneet arvot), ei fysikaaliselta tapahtumalta.",
           "mixed": "Sekä prosessin muutos että mittalaiteongelma ovat edelleen mahdollisia.",
           "unknown": "Näyttö ei puolla prosessia eikä anturia; kyse voi olla häiriöstä, manipuloinnista tai todellisesta tapahtumasta.",
           "point": "Se on häiriö tai manipulointi; pelkästä datasta sitä ei voi päätellä."},
    "sv": {"process": "Flera besläktade givare rörde sig tillsammans och behöll sina vanliga samband, så själva processen ändrades, inte ett enskilt instrument.",
           "sensor": "En givare bröt sitt vanliga samband med sina partnergivare medan de förblev samstämmiga, så instrumentet är den troliga orsaken.",
           "data": "Förändringen ser ut som ett dataproblem (skala, dubbletter, luckor, frysta värden), inte som en fysisk händelse.",
           "mixed": "Både en förändring i processen och ett instrumentproblem är fortfarande möjliga.",
           "unknown": "Underlaget pekar varken på processen eller på en givare; det kan vara en störning, en manipulation eller en verklig händelse.",
           "point": "Det är en störning eller en manipulation; enbart data kan inte avgöra det."},
}
_CAUSE_FIX = {
    "en": {"process": ["Ask the operators what changed around {where}: settings, feed, equipment, maintenance.", "Look at {sensors} on the timeline and compare with the event list of the control system.", "If the change was intended, accept the finding with a note; if not, raise it with the shift leader."],
           "sensor": ["Check {sensor} on site: wiring, power, calibration, fouling, and whether its value is stuck or noisy.", "Compare it with a portable reference or with its partner sensors.", "Until it is fixed, rely on the partner sensors and treat {sensor} as unreliable at {where}."],
           "data": ["Check how {where} was recorded or exported: units, duplicates, gaps, frozen values.", "Fix the data at the source (export, logger, plant database) and run the analysis again.", "Do not treat this as a process fault; set {where} aside."],
           "mixed": ["Check {sensor} first: wiring, calibration, stuck values.", "Then ask the operators what changed around {where}.", "Record what you find by accepting or correcting the finding; the system uses your answer."],
           "unknown": ["Compare {where} with the operators' notes and the event list of the control system.", "Check the sensors that show up most ({sensors}) for wiring and calibration.", "If nothing explains it, question the finding so that it is followed up."]},
    "fi": {"process": ["Kysy operaattoreilta, mikä muuttui ({where}): asetukset, syöttö, laitteet, huolto.", "Katso aikajanalta nämä: {sensors}, ja vertaa ohjausjärjestelmän tapahtumalistaan.", "Jos muutos oli tarkoitettu, hyväksy löydös ja kirjoita huomautus; jos ei, vie asia vuoropäällikölle."],
           "sensor": ["Tarkista paikan päällä {sensor}: johdotus, virta, kalibrointi, likaantuminen ja onko arvo jumissa tai kohiseva.", "Vertaa sitä kannettavaan vertailumittariin tai sen pariantureihin.", "Luota korjaukseen asti pariantureihin ja pidä tätä anturia epäluotettavana ({where})."],
           "data": ["Tarkista, miten rivit ({where}) tallennettiin tai vietiin: yksiköt, kaksoisrivit, aukot, jumiutuneet arvot.", "Korjaa data lähteessä (vienti, tallennin, laitoksen tietokanta) ja aja analyysi uudelleen.", "Älä käsittele tätä prosessivikana; siirrä rivit ({where}) sivuun."],
           "mixed": ["Tarkista ensin {sensor}: johdotus, kalibrointi, jumiutuneet arvot.", "Kysy sitten operaattoreilta, mikä muuttui ({where}).", "Kirjaa havaintosi hyväksymällä tai korjaamalla löydös; järjestelmä käyttää vastaustasi."],
           "unknown": ["Vertaa kohtaa ({where}) operaattoreiden muistiinpanoihin ja ohjausjärjestelmän tapahtumalistaan.", "Tarkista useimmin esiintyvien antureiden ({sensors}) johdotus ja kalibrointi.", "Jos mikään ei selitä sitä, kyseenalaista löydös, jotta se tutkitaan."]},
    "sv": {"process": ["Fråga operatörerna vad som ändrades ({where}): inställningar, matning, utrustning, underhåll.", "Titta på {sensors} på tidslinjen och jämför med styrsystemets händelselista.", "Om ändringen var avsedd, godkänn fyndet med en anteckning; annars ta upp det med skiftledaren."],
           "sensor": ["Kontrollera {sensor} på plats: kablar, ström, kalibrering, nedsmutsning och om värdet har fastnat eller brusar.", "Jämför den med en bärbar referens eller med dess partnergivare.", "Lita på partnergivarna tills den är åtgärdad och betrakta givaren som opålitlig ({where})."],
           "data": ["Kontrollera hur raderna ({where}) registrerades eller exporterades: enheter, dubbletter, luckor, frysta värden.", "Rätta data vid källan (export, logger, anläggningens databas) och kör analysen igen.", "Behandla inte detta som ett processfel; lägg raderna ({where}) åt sidan."],
           "mixed": ["Kontrollera först {sensor}: kablar, kalibrering, fastnade värden.", "Fråga sedan operatörerna vad som ändrades ({where}).", "Dokumentera vad du hittar genom att godkänna eller rätta fyndet; systemet använder ditt svar."],
           "unknown": ["Jämför stället ({where}) med operatörernas anteckningar och styrsystemets händelselista.", "Kontrollera kablar och kalibrering för de givare som förekommer mest ({sensors}).", "Om inget förklarar det, ifrågasätt fyndet så att det följs upp."]},
}
_KIND_STEP = {
    "en": {"drift": "Look for a slow cause: fouling, a leak, a wearing part, a slowly changing feed.",
           "changepoint": "Look for a sudden cause at the start of {where}: a switch, a changed setting, a trip, a valve.",
           "cascade": "Start at {sensor}, where the disturbance began; the other sensors only followed."},
    "fi": {"drift": "Etsi hidasta syytä: likaantuminen, vuoto, kuluva osa, hitaasti muuttuva syöttö.",
           "changepoint": "Etsi äkillistä syytä kohdan ({where}) alusta: kytkin, muutettu asetus, laukeaminen, venttiili.",
           "cascade": "Aloita tästä: {sensor}. Häiriö alkoi siitä; muut anturit vain seurasivat."},
    "sv": {"drift": "Leta efter en långsam orsak: nedsmutsning, ett läckage, en sliten del, en långsamt ändrad matning.",
           "changepoint": "Leta efter en plötslig orsak i början av stället ({where}): en brytare, en ändrad inställning, en utlösning, en ventil.",
           "cascade": "Börja här: {sensor}. Störningen startade där; de andra givarna följde bara efter."},
}
# the first step for a sensor that is probably faulty: the same kind of event points at the instrument, not the process
_KIND_STEP_SENSOR = {
    "en": {"drift": "A slow drift of one sensor usually means fouling, calibration drift or a failing transmitter: check those first.",
           "changepoint": "A sudden jump of one sensor alone usually means a replaced or re-calibrated instrument, a loose connection or a changed range: check what was done to it at the start of {where}."},
    "fi": {"drift": "Yhden anturin hidas ajautuminen johtuu yleensä likaantumisesta, kalibroinnin ryöminnästä tai vikaantuvasta lähettimestä: tarkista ne ensin.",
           "changepoint": "Yhden anturin äkillinen hyppy johtuu yleensä vaihdetusta tai uudelleen kalibroidusta mittalaitteesta, löysästä liitoksesta tai muutetusta mittausalueesta: tarkista, mitä sille tehtiin kohdan ({where}) alussa."},
    "sv": {"drift": "En långsam drift hos en enskild givare beror oftast på nedsmutsning, kalibreringsdrift eller en felande transmitter: kontrollera dem först.",
           "changepoint": "Ett plötsligt hopp hos en enskild givare beror oftast på ett utbytt eller omkalibrerat instrument, en glapp anslutning eller ett ändrat mätområde: kontrollera vad som gjordes med den i början av stället ({where})."},
}
_CAUSE_USE = {"process": "yes", "sensor": "partly", "data": "no", "mixed": "partly", "unknown": "partly"}

_BATCH = {
    "en": {"untrusted": ("Too many sensors of {batch} have data problems ({n}), so the batch as a whole cannot be trusted: a finding here could come from the bad data rather than from the process.",
                         ["Check the sensors named for this batch on site before acting on any finding from these rows.", "Fix the data at the source (export, logger, instruments) and run the analysis again.", "Until then, treat every finding from {batch} as unconfirmed."], "no"),
           "caution": ("{batch} can be used: a few sensors or stretches of rows have problems, and those are set aside automatically.",
                       ["Have a look at the sensors named for this batch when you are on site.", "Use the findings from this batch; the rows that were set aside do not affect them.", "No new export is needed unless the same sensors keep failing."], "partly"),
           "ok": ("{batch} passed every check: the data is complete, in order and within its usual ranges.",
                  ["Nothing to do for the data; go on to the findings.", "Keep the export settings as they are."], "yes")},
    "fi": {"untrusted": ("Liian monessa erän {batch} anturissa on dataongelmia ({n}), joten erään kokonaisuutena ei voi luottaa: löydös voi johtua huonosta datasta eikä prosessista.",
                         ["Tarkista tälle erälle nimetyt anturit paikan päällä ennen kuin toimit näiden rivien löydösten perusteella.", "Korjaa data lähteessä (vienti, tallennin, mittalaitteet) ja aja analyysi uudelleen.", "Pidä siihen asti kaikkia erän {batch} löydöksiä vahvistamattomina."], "no"),
           "caution": ("Erää {batch} voi käyttää: muutamassa anturissa tai rivijaksossa on ongelmia, ja ne siirretään automaattisesti sivuun.",
                       ["Vilkaise tälle erälle nimettyjä antureita, kun olet paikan päällä.", "Käytä tämän erän löydöksiä; sivuun siirretyt rivit eivät vaikuta niihin.", "Uutta vientiä ei tarvita, elleivät samat anturit petä jatkuvasti."], "partly"),
           "ok": ("Erä {batch} läpäisi kaikki tarkistukset: data on täydellistä, järjestyksessä ja tavallisilla alueillaan.",
                  ["Datalle ei tarvitse tehdä mitään; siirry löydöksiin.", "Pidä vientiasetukset ennallaan."], "yes")},
    "sv": {"untrusted": ("För många givare i {batch} har dataproblem ({n}), så satsen som helhet går inte att lita på: ett fynd här kan bero på dåliga data och inte på processen.",
                         ["Kontrollera givarna som nämns för den här satsen på plats innan du agerar på fynd från raderna.", "Rätta data vid källan (export, logger, instrument) och kör analysen igen.", "Betrakta tills dess alla fynd från {batch} som obekräftade."], "no"),
           "caution": ("{batch} kan användas: några givare eller radsträckor har problem, och de läggs automatiskt åt sidan.",
                       ["Titta till givarna som nämns för den här satsen när du är på plats.", "Använd fynden från satsen; raderna som lagts åt sidan påverkar dem inte.", "Ingen ny export behövs om inte samma givare fortsätter att fela."], "partly"),
           "ok": ("{batch} klarade alla kontroller: data är fullständiga, i ordning och inom sina vanliga områden.",
                  ["Inget behöver göras med data; gå vidare till fynden.", "Behåll exportinställningarna som de är."], "yes")},
}


def _build() -> dict[str, dict[str, dict[str, Any]]]:
    lib: dict[str, dict[str, dict[str, Any]]] = {}
    for lang in LANGS:
        d: dict[str, dict[str, Any]] = {}
        for ct, (why, fix, use) in _CHECKS[lang].items():
            d["check." + ct] = {"why": why, "fix": list(fix), "use": use}
        spike, rule, data_fix = d["check.local_spike"], d["check.rule"], _CAUSE_FIX[lang]["data"]
        for cause in CAUSES:
            for kind in FLAG_KINDS:
                what = _KIND_WHAT[lang][kind]
                # "because": the cause sentence alone (the first sentence of "why" only says what happened again); the
                # short Basic-mode strip shows it as the reason
                if kind == "point":
                    e = {"why": what + " " + _CAUSE_WHY[lang]["point"], "because": _CAUSE_WHY[lang]["point"], "fix": list(spike["fix"]), "use": "partly"}
                elif kind == "dq":
                    e = {"why": what + " " + _CAUSE_WHY[lang]["data"], "because": _CAUSE_WHY[lang]["data"], "fix": list(data_fix), "use": "partly"}
                elif kind == "rule":
                    e = {"why": what + " " + _CAUSE_WHY[lang][cause], "because": _CAUSE_WHY[lang][cause], "fix": list(rule["fix"]), "use": "yes"}
                else:
                    # the kind's first step fits the cause: a faulty sensor gets the instrument version, a data problem none
                    step = None if cause == "data" else (_KIND_STEP_SENSOR[lang].get(kind) if cause == "sensor" else None) or _KIND_STEP[lang].get(kind)
                    e = {"why": what + " " + _CAUSE_WHY[lang][cause], "because": _CAUSE_WHY[lang][cause], "fix": (([step] if step else []) + list(_CAUSE_FIX[lang][cause]))[:MAX_FIX], "use": _CAUSE_USE[cause]}
                d[f"diagnosis.{cause}.{kind}"] = e
        for bk, (why, fix, use) in _BATCH[lang].items():
            d["batch." + bk] = {"why": why, "fix": list(fix), "use": use}
        lib[lang] = d
    return lib


LIBRARY = _build()

_DUP = {"duplicate", "duplicate_rows"}


def check_key(check_type: Any) -> str:
    ct = str(check_type or "")
    if ct.startswith("rule"):
        return "rule"
    if ct in _DUP:
        return "duplicate_rows"
    if ct in ("duplicate_ts",):
        return "duplicate_timestamp"
    return ct if ct in CHECK_TYPES else "other"


def _lang(lang: Optional[str]) -> str:
    code = (lang or "en").lower()[:2]
    return code if code in LANGS else "en"


def library_key(kind: str, subtype: Any) -> str:
    kind = {"diag": "diagnosis", "flag": "diagnosis", "trust": "batch"}.get(str(kind), str(kind))
    if kind == "check":
        return "check." + check_key(subtype)
    if kind == "batch":
        s = str(subtype or "caution")
        return "batch." + (s if s in BATCH_KINDS else "caution")
    if isinstance(subtype, (tuple, list)):
        cause, fkind = (list(subtype) + ["", ""])[:2]
    else:
        cause, _, fkind = str(subtype or "").replace(":", ".").partition(".")
    cause = cause if cause in CAUSES else "unknown"
    fkind = fkind if fkind in FLAG_KINDS else "anomaly"
    return f"diagnosis.{cause}.{fkind}"


def _fill(text: str, facts: dict[str, Any], lang: str) -> str:
    dflt = DEFAULTS[lang]

    def rep(m: "re.Match[str]") -> str:
        v = facts.get(m.group(1))
        return str(v) if v not in (None, "", []) else dflt.get(m.group(1), "")

    s = re.sub(r"\{(\w+)\}", rep, text)
    s = re.sub(r"\s*\(\s*\)", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # "rivit (rivit 120–180)" / "raderna (rader 5–9)": the noun in front of the bracket already says it
    s = re.sub(r"\b(\w{3})(\w*) \((\w+) (?=\d)", lambda m: f"{m.group(1)}{m.group(2)} (" if m.group(3).lower().startswith(m.group(1).lower()) else m.group(0), s)
    # a sentence that starts with a sensor's own name keeps the name's spelling
    lead = re.match(r"\s*\{(\w+)\}", text)
    return s if lead and facts.get(lead.group(1)) not in (None, "", []) else s[:1].upper() + s[1:]


def advice_for(kind: str, subtype: Any, facts: Optional[dict[str, Any]] = None, lang: str = "en") -> dict[str, Any]:
    """{"why": str, "fix": [2-4 steps], "can_use_rows": "yes" | "no" | "partly", "key": library key}."""
    lang = _lang(lang)
    key = library_key(kind, subtype)
    e = LIBRARY[lang].get(key) or LIBRARY["en"][key]
    f = dict(facts or {})
    if isinstance(f.get("sensors"), (list, tuple)):
        f["sensors"] = ", ".join(str(x) for x in f["sensors"][:3])
    if not f.get("sensors") and f.get("sensor"):
        f["sensors"] = f["sensor"]
    out = {"why": _fill(e["why"], f, lang), "fix": [_fill(s, f, lang) for s in e["fix"]][:MAX_FIX], "can_use_rows": e["use"], "key": key}
    if e.get("because"):
        out["because"] = _fill(e["because"], f, lang)
    return out


# ------------------------------------------------------------------------------------------------ objects of a run
def advice_for_object(ws: Any, kind: str, obj: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    """Advice for a check / flag / diagnosis / trust record: subtype and facts are read from the record."""
    from . import brief as B
    from .plain import _read

    lang = _lang(lang)
    names = B._signal_names(ws)
    nm = lambda s: names.get(str(s), str(s))  # noqa: E731
    kind = {"diag": "diagnosis", "trust": "batch"}.get(kind, kind)
    if kind == "check":
        sigs = [str(s) for s in (obj.get("signals") or [])]
        rows = B._rows(obj.get("row_start"), obj.get("row_end") if obj.get("row_end") is not None else obj.get("row_start"), lang) if obj.get("row_start") is not None else ""
        facts = {"sensor": nm(sigs[0]) if sigs else "", "sensors": [nm(s) for s in sigs], "rows": rows, "where": rows, "batch": obj.get("batch_id")}
        out = advice_for("check", obj.get("check_type"), facts, lang)
        if obj.get("status") == "pass" or str(obj.get("check_type") or "").endswith("_ok"):
            out = {**advice_for("batch", "ok", facts, lang), "key": "batch.ok"}
        return out
    if kind == "batch":
        issue = bool(obj.get("reasons") or obj.get("local_untrusted") or obj.get("untrusted_signals"))
        level = "untrusted" if not obj.get("trusted") else "caution" if issue else "ok"
        sigs = [str(s) for s in (obj.get("untrusted_signals") or [])]
        n = len(sigs) or len({l.get("signal") for l in (obj.get("local_untrusted") or []) if isinstance(l, dict)})
        return advice_for("batch", level, {"batch": obj.get("batch_id"), "n": n or "", "sensor": nm(sigs[0]) if sigs else "", "sensors": [nm(s) for s in sigs]}, lang)
    if kind == "flag":
        sigs = [str(r["signal"]) for r in (obj.get("signals_ranked") or []) if isinstance(r, dict) and r.get("signal")]
        facts = {"sensor": nm(sigs[0]) if sigs else "", "sensors": [nm(s) for s in sigs], "where": B._flag_where(obj, lang), "rows": B._flag_where(obj, lang), "batch": obj.get("batch_id")}
        return advice_for("flag", (obj.get("likely_cause_class"), obj.get("kind")), facts, lang)
    # diagnosis: the flag kind comes from its strongest flag
    flags_by_id = {f.get("id"): f for f in (_read(ws, "flags.jsonl", []) or []) if isinstance(f, dict)}
    place = B._diag_place(obj, flags_by_id)
    fkind = "point" if B._is_points_diag(obj, flags_by_id) else str((place or {}).get("kind") or "anomaly")
    sigs = [str(r["signal"]) for r in (obj.get("ranked_signals") or []) if isinstance(r, dict) and r.get("signal")]
    where = B._flag_where(place, lang) if place else ""
    facts = {"sensor": nm(sigs[0]) if sigs else "", "sensors": [nm(s) for s in sigs], "where": where, "rows": where, "batch": (place or {}).get("batch_id")}
    return advice_for("diagnosis", (obj.get("cause_class"), fkind), facts, lang)


def find_object(ws: Any, kind: str, object_id: str) -> Optional[dict[str, Any]]:
    from .evidence_plain import _canonical
    from .plain import _read

    cid = _canonical(object_id)
    src = {"check": ("checks.jsonl", "check_id"), "flag": ("flags.jsonl", "id"), "diagnosis": ("diagnoses.jsonl", "id"), "batch": ("trust.jsonl", "batch_id")}.get(kind)
    if not src:
        return None
    return next((x for x in (_read(ws, src[0], []) or []) if isinstance(x, dict) and x.get(src[1]) == cid), None)


def kind_of_id(object_id: str) -> Optional[str]:
    s = str(object_id or "").strip().upper()
    if s.startswith("CHK"):
        return "check"
    if s.startswith("FLAG"):
        return "flag"
    if s.startswith("DIAG"):
        return "diagnosis"
    return "batch" if re.fullmatch(r"B\d{4,6}", s) else None
