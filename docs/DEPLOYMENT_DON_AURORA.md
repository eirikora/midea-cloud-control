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
  -czf /home/tommy/smarthome/backups/homeassistant-before-mcc-0.2.0.tar.gz \
  homeassistant
docker compose -f /home/tommy/smarthome/compose.yaml start <ha-service>
```

Verifiser at arkivet kan listes med `tar -tzf` før videre arbeid. Ta i tillegg
en separat kopi av eksisterende
`custom_components/midea_cloud_control` for rask komponent-rollback.

## 3. Klargjør artefakt

Pakk bare runtime-mappen fra den gjennomgåtte lokale commit-en. Ikke kopier
`.venv`, tester, Git-metadata eller lokale logger.

```bash
tar -czf midea-cloud-control-0.2.0.tar.gz \
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
   - forventet polling omtrent hvert 90–120 sekund;
   - enheten blir midlertidig unavailable ved testet nettbrudd og kommer tilbake.
3. Etter eksplisitt godkjenning, send én ufarlig temperaturendring til denne ene
   enheten og sammenlign med NetHome Plus-appen.
4. Aktiver deretter 3, 5 og til slutt 15 enheter, med observasjon mellom hvert
   trinn. Stopp oppskaleringen ved login-loop, økende 9999-rate eller feil enhet.

## 5. Konsolider gamle oppføringer

Gamle v1-oppføringer deler allerede én runtime-sesjon etter migrering, men har
fortsatt hver sin lagrede passordkopi. Når stabilitet og entity mapping er
bekreftet:

1. Velg én migrert oppføring som primær.
2. Bruk **Configure** og velg alle 15 appliances.
3. Hold de 14 andre oppføringene deaktivert mens entity IDs og device registry
   kontrolleres. Ikke slett dem ved første test.
4. Etter stabil drift og verifisert mapping kan de overflødige oppføringene
   fjernes. Da gjenstår én passordkopi og én kontooppføring.

Hvis Home Assistant varsler om dupliserte unique IDs under konsolideringen, stopp
og bruk rollback i stedet for å redigere `.storage` manuelt.

## 6. Rollback

Ved alvorlig feil:

1. Deaktiver integrasjonen og stopp `<ha-service>`.
2. Gjenopprett hele backup-arkivet til `/home/tommy/smarthome`, eller gjenopprett
   kun den separate gamle component-mappen dersom config/entity registry ikke
   er endret.
3. Start containeren og bekreft at Home Assistant laster normalt.
4. La integrasjonen være deaktivert. NetHome Plus-appen skal fortsatt kunne
   brukes uavhengig.

Ikke slett backupen før alle 15 enheter har vært stabile gjennom en avtalt
observasjonsperiode.
