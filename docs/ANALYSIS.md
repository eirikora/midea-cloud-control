# Teknisk analyse

## Verifiserte funn

Utgangspunktet er upstream-commit `4e29c35`.

1. `async_setup_entry()` opprettet en ny `MideaCloudClient` og kalte `login()`
   for hver config entry. Med 15 enhetsoppføringer ga dette 15 separate
   innlogginger og 15 uavhengige sesjoner for samme konto.
2. Hver coordinator pollet hvert 30. sekund. Det fantes ingen felles kø,
   samtidighetsgrense, ratebegrensning eller spredning mellom enhetene.
3. `transparent_send()` kalte den private `midea-local`-metoden `_api_request()`
   uten `raise_for_error=True`. I `midea-local 12.2.0` blir 3106 og 3144 bare
   kastet som `MideaCloudError` når dette flagget er satt. Ellers logges feilen
   og integrasjonen mottar `None`, slik at den ikke kan skille utløpt sesjon fra
   andre tomme svar.
4. NetHome Plus bruker `MideaAirCloud`. Etter login lagrer objektet både access
   token og `sessionId`, og `_make_general_data()` legger session-ID-en i senere
   kall. Å kalle `login()` på samme utløpte objekt kan derfor sende gammel
   session-ID i login-flyten. Sesjonsfornyelse må bygge et nytt skyobjekt.
5. `midea-local 12.2.0` klassifiserer 3106 og 3144 som
   `cloud_session_expired`, 9999 som transient `cloud_error`, og 65027 som
   `too_many_logged_in_devices`. Biblioteket prøver 9999 opptil tre ganger med
   kort backoff før det returnerer tomt svar.
6. Biblioteket har en intern `_api_lock` per skyobjekt, men den opprinnelige
   integrasjonen opprettet ett skyobjekt per enhet. Låsen ga derfor ingen felles
   begrensning på tvers av de 15 oppføringene.

## Rotårsak og usikkerhet

Den konkrete NetHome Plus-regelen for samtidige sesjoner er ikke dokumentert og
er ikke testet mot brukerens konto. Det er derfor ikke bevist at hver ny login
alltid ugyldiggjør forrige sesjon. Feilkode 65027 viser at tjenesten har en form
for innloggingsgrense, men beviser ikke terskelen eller om 3106 skyldtes akkurat
den grensen.

Det som er sikkert, er at gammel arkitektur forsterket enhver slik begrensning:
15 innlogginger under oppstart, ingen koordinert fornyelse og synkrone 30-sekunds
pollinger. `3144` kan skyldes server-side login-state/ratebegrensning, og ved
reauth kan gjenbruk av et objekt med gammel `sessionId` forverre feilen.

## Implementert løsning

- Ett delt klientobjekt per `(cloud, normalisert konto)` med referansetelling.
- Options flow bruker en midlertidig registry-lease. En deaktivert oppføring
  gjenbruker dermed en aktiv kontoøkt som andre oppføringer holder, og åpning av
  **Configure** utfører ikke ny login når sesjonen fortsatt er gyldig.
- Login-lås slik at parallelle oppsett bare utfører én reell innlogging.
- Nytt `MideaAirCloud`-objekt ved reauth; én kontrollert fornyelse per request.
- Maksimalt tre loginforsøk, to ytre requestforsøk, eksponentiell backoff med
  jitter og 60 sekunders cooldown etter uttømte loginforsøk.
- Felles semaphore (2) og minimum 0,75 sekunder mellom request-starter.
- 90 sekunders standardpolling, deterministisk spredt opp til 120 sekunder.
- Konto-basert config entry med flere climate-entiteter og passord lagret én
  gang for nye oppsett.
- Forsiktig v1-migrering som beholder appliance-ID som entity unique ID. Gamle
  oppføringer deler sesjon med en gang, men automatisk sletting/sammenslåing er
  med hensikt unngått.
- Delvis tilgjengelighet: én offline enhet stopper ikke de andre enhetene i
  samme kontooppføring fra å lastes.

## Begrensninger

- Transparent-send bygger på private `midea-local`-metoder. Versjon 12.2.0 er
  derfor låst i manifestet; en biblioteksoppgradering må gjennomgås eksplisitt.
- Bibliotekets egne tre HTTP-forsøk ligger under integrasjonens ytre retry. Det
  totale antallet er fortsatt begrenset, men ett logisk kall kan gi flere
  nettverksforsøk enn integrasjonens teller alene viser.
- Ingen automatisk test kontakter NetHome Plus. Faktisk skyadferd og kommandoer
  må valideres gradvis i produksjonsmiljøet etter eksplisitt godkjenning.
