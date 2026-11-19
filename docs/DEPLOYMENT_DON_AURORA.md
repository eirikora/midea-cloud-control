# Trygg utrulling til don-aurora

Dette er en runbook, ikke en utført deploy. Ingen filer på `don-aurora` er
endret.

## 1. Før godkjenning

1. Gjennomgå diffen og de lokale testresultatene.
2. Kontroller Home Assistant-versjon og Python-versjon i containeren. Python må
   være 3.12 eller nyere for `midea-local 12.2.0`.
3. Finn Compose-servicenavnet uten å anta at det er `homeassistant`:

   ```bash
   docker compose -f /home/tommy/smarthome/compose.yaml config --services
   ```

4. Behold integrasjonen deaktivert til backup og filkopiering er ferdig.

## 2. Konsistent backup

Sett inn det verifiserte servicenavnet for `<ha-service>`. Stopp containeren før
full backup slik at `.storage` og entity registry er konsistente.

```bash
mkdir -p /home/tommy/smarthome/backups
docker compose -f /home/tommy/smarthome/compose.yaml stop <ha-service>
tar --xattrs --acls -C /home/tommy/smarthome \
  -czf /home/tommy/smarthome/backups/homeassistant-before-mcc-0.2.3.tar.gz \
  homeassistant
docker compose -f /home/tommy/smarthome/compose.yaml start <ha-service>
```

Verifiser at arkivet kan listes med `tar -tzf`, og at det inneholder
config entry-lagringen, før videre arbeid:

```bash
tar -tzf /home/tommy/smarthome/backups/homeassistant-before-mcc-0.2.3.tar.gz \
  | grep -E 'homeassistant/\.storage/core\.config_entries$'
```

Ingen treff betyr at backupen **ikke** kan brukes til rollback (se avsnitt 6).
Ta i tillegg en separat kopi av eksisterende
`custom_components/midea_cloud_control`. Den er bare nyttig *før* v0.2.3 har
startet (se avsnitt 6).

## 3. Klargjør artefakt

Pakk bare runtime-mappen fra den gjennomgåtte lokale commit-en. Ikke kopier
`.venv`, tester, Git-metadata eller lokale logger.

```bash
tar -czf midea-cloud-control-0.2.3.tar.gz \
  custom_components/midea_cloud_control
```

Overfør arkivet til en midlertidig mappe på serveren, kontroller checksum, stopp
Home Assistant, erstatt komponentmappen, og start containeren igjen. Disse
stegene skal først utføres etter eksplisitt deploy-godkjenning.

## 4. Trinnvis funksjonstest

1. Aktiver én eksisterende config entry. Migreringen gjør den til en
   én-enhets kontooppføring uten å endre climate unique ID.
2. Observer minst 10 pollsykluser uten å sende kommandoer. Bekreft:
   - én login ved oppstart;
   - ingen gjentatte 3106/3144;
   - stabil temperatur/status;
   - forventet polling omtrent hvert konfigurerte intervall (105 sekunder som
     standard), med enhetene jevnt faseforskjøvet;
   - siste tilstand beholdes ved to korte nettbrudd; vedvarende feil gjør
     enheten unavailable, og den kommer tilbake etter en vellykket poll.
3. Etter eksplisitt godkjenning, send én ufarlig temperaturendring til denne ene
   enheten og sammenlign med NetHome Plus-appen.
4. Aktiver deretter 3, 5 og til slutt 16 enheter, med observasjon mellom hvert
   trinn. Stopp oppskaleringen ved login-loop, økende 9999-rate eller feil enhet.

## 5. Konsolider gamle oppføringer

Gamle v1-oppføringer deler allerede én runtime-sesjon etter migrering, men har
fortsatt hver sin lagrede passordkopi. Når stabilitet og entity mapping er
bekreftet:

1. Velg én migrert oppføring som primær.
2. Bruk **Configure** og velg alle 16 appliances.
3. Hold de 15 andre oppføringene deaktivert mens entity IDs og device registry
   kontrolleres. Ikke slett dem ved første test.
4. Etter stabil drift og verifisert mapping kan de overflødige oppføringene
   fjernes. Da gjenstår én passordkopi og én kontooppføring.

Hvis Home Assistant varsler om dupliserte unique IDs under konsolideringen, stopp
og bruk rollback i stedet for å redigere `.storage` manuelt.

## 6. Rollback

Ved alvorlig feil:

> **Viktig – skjemaversjon.** Første gang v0.2.3 starter, migrerer den alle
> Midea Cloud Control-oppføringer til config entry-versjon 3 og skriver dette
> til `.storage/core.config_entries`. Eldre versjoner (v0.2.2 og tidligere,
> skjema 2) kan ikke lese versjon 3. Home Assistant nekter å nedgradere, og
> integrasjonen lastes da ikke. Fra dette tidspunktet er det **ikke** nok å
> legge tilbake den gamle komponentmappen.

1. Deaktiver integrasjonen og stopp `<ha-service>`.
2. Velg rollback-metode:
   - **v0.2.3 har startet minst én gang** (det normale tilfellet): gjenopprett
     hele backup-arkivet fra avsnitt 2 til `/home/tommy/smarthome`. Endringer
     gjort i Home Assistant etter backupen går tapt.
   - **v0.2.3 har aldri startet** (for eksempel feil under filkopiering):
     det holder å gjenopprette den separate gamle komponentmappen.
   Ikke rediger `.storage/core.config_entries` for hånd for å sette
   versjonen tilbake.
3. Start containeren og bekreft at Home Assistant laster normalt, og at
   oppføringene ikke står som «Migration error».
4. La integrasjonen være deaktivert. NetHome Plus-appen skal fortsatt kunne
   brukes uavhengig.

Ikke slett backupen før alle 16 enheter har vært stabile gjennom en avtalt
observasjonsperiode.
