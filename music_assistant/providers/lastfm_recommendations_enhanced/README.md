# Last.fm Recommendations Enhanced

Persoenliche Empfehlungen aus Music Assistant (MA) und allgemeine Last.fm-Charts,
Genre-Auswahlen sowie dynamische Playlists. Die Wiedergabe erfolgt ueber MA;
Bibliothek, Favoriten und Hoerverlauf werden nicht veraendert.

## Voraussetzungen

- Ein eigener [Last.fm API-Key](https://www.last.fm/api/account/create).
  Kein mitgelieferter Key, Last.fm-Login, Benutzername oder API-Secret erforderlich.
- Einen aktivierten MA-Nutzer im Setup auswaehlen. Seine Hoerdaten, Favoriten
  und verfuegbaren Musikquellen bilden die persoenliche Grundlage; die Reihen
  sind nur fuer diesen Nutzer sichtbar. Die Nutzerwahl kann durch erneutes
  Ausfuehren des Setups unter "Music Assistant user for recommendations"
  (`recommendation_user_id`) geaendert werden.

## Betrieb und Hinweise

- Aufbau 20 Sekunden nach dem Start, danach alle 24 Stunden im Hintergrund.
  Abrufe liefern den letzten vollstaendigen Stand; bis zum ersten Aufbau ist
  die Liste leer. Bei Fehlern bleibt der vorherige Stand erhalten.
  Unvollstaendige Aufloesungen werden fruehestens nach 5 Minuten erneut versucht.
  Zufallsauswahlen entstehen nur beim Aufbau oder Refresh, nicht beim Oeffnen.
- Nur ueber die sichtbaren MA-Musikquellen aufloesbare Medien werden ausgegeben.
  Begrenzte Kandidatenlisten, Verfuegbarkeit und Filter koennen zu kuerzeren
  oder leeren Reihen fuehren; es gibt keine Katalogdurchlaeufe.
- Alle Instanzen teilen ein Limit von 5 Last.fm-Anfragen pro Sekunde.
  Bei Rate Limits gilt ein gemeinsamer Cooldown von mindestens 60 Sekunden;
  ein laengeres `Retry-After` hat Vorrang, auch bei Wiederholungen.
- Playlists sind dynamisch und nicht editierbar. Sie werden nicht als dauerhafte
  MA-Playlists angelegt; Links koennen bei geaendertem Profil ungueltig werden.

Es gelten die [Last.fm-API-Nutzungsbedingungen](https://www.last.fm/api/tos).
Last.fm liefert keine Streamingrechte; Audio stammt aus den MA-Musikquellen.

## Folder und Herleitung

MA-`week` umfasst die letzten 7 Tage, MA-`month` die letzten 30 Tage ab dem
Hintergrundaufbau, keine Kalenderperioden. MA-Tracks und MA-Kuenstler umfassen
auch Medienidentitaeten von Streaming-Anbietern. Ihre Hoerdaten und Favoriten
stammen aus MA; Tags und Listenraenge aus Last.fm, wie in der Tabelle angegeben.
Rotationsausschluss entfernt Identitaeten aus bis zu 500 MA-Monats-Top-Items.

**MA-Seedgewichtung:** Der Hoerscore der MA-Tracks bzw. MA-Kuenstler ist
`MA-Monats-Plays + 2 * MA-Wochen-Plays + 0.1 * MA-Allzeit-Plays`.
Die Scores werden durch den hoechsten Score der Auswahl auf 0 bis 1 normalisiert;
bei Maximum 0 bleiben sie 0. Danach kommen fuer MA-Favoriten 0.2 hinzu.
MA-Favoriten ohne Plays erhalten damit 0.2; der MA-Seedscore kann 1.2 erreichen.
Nur verfuegbare, eindeutige MA-Medien werden als Seeds verwendet.

**MA-Track-Seeds** kombinieren MA-Top-Tracks aus `month`, `week` und `all_time`
mit MA-Track-Favoriten. Die MA-Seedgewichtung bestimmt die Reihenfolge.
**MA-Kuenstler-Seeds** entstehen entsprechend aus MA-Top-Kuenstlern und
MA-Kuenstler-Favoriten. Die Anzahl der verwendeten Seeds steht je Sammlung
in der Tabelle.

**Seed-Wiederholungen:** In den Ergebnissen seedbasierter Empfehlungen duerfen MA-Seed-Medien
hoechstens 20% des jeweiligen Ergebnismaximums belegen, unabhaengig von der
tatsaechlichen Listenlaenge. Auch kuerzlich gehoerte Medien bleiben zulaessig.
Nur dieselben Medien zaehlen, nicht andere Tracks oder Alben eines Kuenstler-Seeds.

**Last.fm-Aehnlichkeitsgewichtung:** In Empfehlungen erhaelt ein Last.fm-Treffer
`MA-Seedscore * Last.fm-match`. Artist-Matches werden direkt verwendet;
Track-Matches werden je Antwort auf deren staerksten gueltigen Match normalisiert,
damit unterschiedliche Last.fm-Skalen das MA-Seedgewicht nicht ueberlagern.
Bei ausschliesslich 0 bleiben die Matches 0. Fuer fehlende oder ungueltige Matches
ersetzt `1 / Last.fm-Listenrang` den Match, nicht aber fuer einen gueltigen Match 0.
Beitraege gleicher Treffer werden addiert,
die fertigen Summen normalisiert und absteigend sortiert. Der geerbte
MA-Favoritenbonus wird nicht erneut addiert.

**Zufallsauswahl ohne Quoten:** Bei Empfehlungs- und Entdeckungslisten bilden die
besten `min(3 * Ergebnismaximum, vorhandene Kandidaten)` nach der jeweiligen
Gewichtung den Auswahlpool. Diesen zufaellig mischen und bis zum Ergebnismaximum
aufloesen. Charts und MA-Meistgehoert-Reihen behalten dagegen ihre Rangfolge.
Bei Rediscovery erst den Monats-Ausschluss anwenden, dann bis zu 50 Tracks zufaellig
aus den besten 150 verbleibenden historischen Tracks auswaehlen.
Die vorbereitete Auswahl bleibt bis zum naechsten erfolgreichen Aufbau unveraendert;
Zufall reduziert Wiederholungen, garantiert aber keine anderen Ergebnisse.

**Persoenliche Kuenstler-Playlists:** Die MA-Seedgewichtung bestimmt die Playlist-Auswahl.
Innerhalb jeder Playlist bestimmen gueltige Last.fm-`match`-Scores die Beitraege;
der Seed selbst erhaelt Score 1. Eindeutige Kuenstler absteigend nach Score bearbeiten,
ohne zusaetzliche Kuenstlergrenze. Die Trackquote ist
`max(1, ceil(50 * Kuenstlerscore / Summe der Kuenstlerscores))`.
Erst fuer den aktuellen Kuenstler dessen Last.fm-Top-Tracks abrufen, die besten
`min(3 * Trackquote, 25)` zufaellig mischen und bis zur Quote auf MA-Quellen aufloesen.
Danach erst den naechsten Kuenstler abfragen; bei 50 Tracks sofort stoppen.
Die Quote ist eine Obergrenze, kein garantierter Beitrag, und wird nicht ueberschritten.
Der Auswahlpool begrenzt den Beitrag eines Kuenstlers auf hoechstens 25 Tracks.
Die fertige Playlist nochmals zufaellig mischen. Kein eigener MA-Track-Favoritenbonus.

**Globale Kuenstler-Radios:** Der Chart-Ausgangskuenstler erhaelt Score 1;
hoechstens 2 eindeutige Nachbarn werden nach Last.fm-Match ausgewaehlt.
Fuer fehlende Matches gilt der Listenrang-Ersatz. Erst nach dieser Auswahl
Trackquoten aus der Summe der ausgewaehlten Scores berechnen. Zufallsauswahl,
sequenzielle Aufloesung und abschliessendes Mischen wie bei den persoenlichen
Kuenstler-Playlists; keine zusaetzlichen Kuenstler-Abfragen.

**Persoenliches Genreprofil:** MA-Kuenstler-Seeds liefern Last.fm-Tags aus
`artist.getTopTags`, aus denen bereinigte Genres ausgewaehlt werden.
Last.fm-counts oder ersatzweise `1 / Last.fm-Tagrang` bestimmen die Taggewichte.
Diese werden je Kuenstler auf Genreanteile mit Summe 1 verteilt und mit dessen
MA-Seedscore multipliziert. Beitraege je Genre addieren und normalisieren;
die staerksten Genres bestimmen die persoenlichen Genre-Reihen und Playlists.

**Verwandtes Genreprofil:** Je Ausgangskuenstler den staerksten Last.fm-Nachbarn
aus `artist.getSimilar` auswaehlen. Er erhaelt `MA-Seedscore * Last.fm-match`
mit dem beschriebenen Listenrang-Ersatz. Seine Last.fm-Tags werden wie im
persoenlichen Genreprofil gewichtet. Hauptprofil-Genres entfallen;
die staerksten verbleibenden Genres werden ausgewaehlt.

**Persoenliche Album-Auswahl:** MA-Kuenstler-Seeds inklusive MA-Kuenstler-Favoriten
liefern Last.fm-Nachbarn aus `artist.getSimilar`. Jeder Seed erhaelt fuer sich selbst
Match 1; Nachbarn erhalten den Last.fm-Match mit dem beschriebenen Listenrang-Ersatz.
Je Seed gleiche Kuenstler nur einmal beruecksichtigen und Beitraege aus
`MA-Seedscore * Match` seeduebergreifend addieren. Alle Kuenstler absteigend nach
Gesamtscore bearbeiten, ohne Begrenzung auf 3 Albumquellen.
Die Albumquote ist `max(1, ceil(Ergebnismaximum * Kuenstlerscore / Summe der Kuenstlerscores))`.
Erst fuer den aktuellen Kuenstler `artist.getTopAlbums` abrufen, dessen beste
`min(3 * Albumquote, 10)` Alben als Auswahlpool zufaellig mischen und daraus
bis zur Albumquote eindeutige Alben auf MA-Quellen aufloesen.
Die Quote ist eine Obergrenze, kein garantierter Beitrag; ein erschoepfter Pool beendet
die Auswahl fuer diesen Kuenstler.
Bei 35 Alben stoppen; die fertige Auswahl zufaellig mischen.
Es gibt keine eigenen MA-Album-Seeds, Albumscores oder MA-Album-Favoritenboni.

**Alben beliebter Kuenstler:** Bis zu 10 staerkste Chart-Kuenstler bilden die Quellen.
Jeder erhaelt die gleiche Albumquote `ceil(35 / Anzahl der Quellen)`.
Je Kuenstler die besten `min(3 * Albumquote, 10)` Alben zufaellig mischen und bis
zur Quote aufloesen; erst danach den naechsten Kuenstler abfragen. Bei 35 Alben stoppen
und die fertige Auswahl nochmals mischen. Keine persoenlichen Scores oder Favoritenboni.

**Last.fm-Abfragen und Auffuellen:** Last.fm-Abfragen senden `limit=50`, ausser
Taglisten fuer die groesseren Zufallspools: `tag.getTopTracks` bis zu 150,
`tag.getTopArtists` bis zu 75 und `tag.getTopAlbums` bis zu 105 Kandidaten.
Je benoetigter Liste bleibt es bei einer gecachten, gedrosselten Anfrage ohne Paging.
Lokale Auswahlgrenzen fuer Genres und Ausgangskuenstler bleiben bestehen.
Kandidaten werden in ihrer vorbereiteten Rangfolge einzeln auf MA-Quellen
aufgeloest. Fehlende, doppelte oder ausgeschlossene Treffer lassen den naechsten
Kandidaten nachruecken. Die Aufloesung endet beim Ergebnismaximum oder einem
erschoepften Kandidatenpool, nicht nach einer festen Anzahl fehlender Treffer.
Rate Limits und voruebergehende Quellenfehler bleiben vom normalen Nachruecken getrennt.

| Folder-ID | Titel | Inhalt | Herleitung |
| --- | --- | --- | --- |
| `user_recommendations` | Track-Empfehlungen | Bis zu 50 persoenlich empfohlene Tracks. | Bis zu 15 MA-Track-Seeds inklusive MA-Favoriten; je bis zu 50 Last.fm-Treffer aus `track.getSimilar`, nach Last.fm-Aehnlichkeitsgewichtung sortiert. Eindeutige Kandidaten als Zufallspool vorbereiten und auf MA-Quellen aufloesen. |
| `user_genre_playlists_monthly` | Deine Genre-Playlists | Bis zu 10 Playlists der staerksten persoenlichen Genres mit je bis zu 50 Tracks. | Bis zu 15 MA-Kuenstler-Seeds mit je maximal 5 bereinigten Last.fm-Genres; staerkste Genres nach Genregewicht auswaehlen. Bis zu 150 Tracks aus `tag.getTopTracks` nach normalisiertem Listenrang plus 0.2 MA-Track-Favoritenbonus als Zufallspool vorbereiten; maximal 10 Tracks pro Kuenstler. |
| `user_top_artists_monthly` | Deine meistgehoerten Kuenstler | Bis zu 50 Kuenstler im Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=artist`, `period=month`. Play-Rangfolge erhalten, unverfuegbare und doppelte Identitaeten entfernen; keine Favoritenboni oder Last.fm-Daten. |
| `user_top_tracks_monthly` | Deine meistgehoerten Tracks | Bis zu 50 Tracks im Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=track`, `period=month`. Play-Rangfolge erhalten, unverfuegbare und doppelte Identitaeten entfernen; keine Favoritenboni oder Last.fm-Daten. |
| `user_artist_recommendations` | Kuenstler-Empfehlungen | Bis zu 25 passende, bislang weniger gehoerte Kuenstler. | Bis zu 10 MA-Kuenstler-Seeds inklusive MA-Favoriten; je bis zu 50 Last.fm-Treffer aus `artist.getSimilar`, nach Last.fm-Aehnlichkeitsgewichtung sortiert. Eindeutige Kandidaten als Zufallspool vorbereiten und auf MA-Quellen aufloesen. |
| `user_artist_playlists_monthly` | Deine Kuenstler-Playlists | Bis zu 10 dynamische Kuenstler-Playlists mit je bis zu 50 Tracks, etwa Featuring Radiohead. | Bis zu 10 MA-Kuenstler-Seeds inklusive MA-Favoriten bestimmen die Playlists. Je Seed bis zu 50 Last.fm-Nachbarn aus `artist.getSimilar`; der Seed selbst erhaelt Score 1, Nachbarn ihren Match-Score. Daraus Trackquoten berechnen. Je benoetigtem Kuenstler bis zu 50 Tracks aus `artist.getTopTracks` abrufen, die besten `min(3 * Trackquote, 25)` zufaellig mischen und bis zur Quote auf MA-Quellen aufloesen; bei 50 Tracks stoppen und die Playlist mischen. |
| `user_album_recommendations` | Album-Empfehlungen | Bis zu 35 zum persoenlichen Geschmack passende Alben. | Bis zu 10 MA-Kuenstler-Seeds inklusive MA-Favoriten, je bis zu 50 Last.fm-Nachbarn. Kuenstler nach kombiniertem Seed-/Match-Score bearbeiten; je Kuenstler bis zur Albumquote zufaellig aus dessen besten `min(3 * Albumquote, 10)` Alben auswaehlen und auf MA-Quellen aufloesen. |
| `user_top_albums_monthly` | Deine meistgehoerten Alben | Bis zu 50 Alben im Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=album`, `period=month`; beruht auf MA-Zuordnungen gehoerter Tracks zu Alben. Play-Rangfolge erhalten; keine Album-Favoritenboni oder Last.fm-Daten. |
| `user_rediscovery_tracks` | Lange nicht gehoert | Bis zu 50 frueher haeufig gehoerte Tracks ausserhalb der aktuellen Rotation. | Nur MA-`statistics/top_items`, ohne MA-Favoritenbonus oder Last.fm: bis zu 500 MA-Allzeit-Top-Tracks minus bis zu 500 MA-Monats-Top-Tracks nach URI. Bis zu 50 zufaellig aus den besten 150 verbleibenden verfuegbaren historischen Track-Identitaeten waehlen. Bei 500 MA-Monats-Treffern leer. |
| `user_genre_artists_{genre_key}` | Kuenstler aus deinem Lieblingsgenre: {Genre} | Bis zu 3 Reihen persoenlicher Schwerpunktgenres mit je bis zu 25 Kuenstlern. | Bis zu 15 MA-Kuenstler-Seeds inklusive MA-Favoriten mit je maximal 5 bereinigten Last.fm-Genres; die 3 staerksten Genres nach Genregewicht auswaehlen. Je Genre bis zu 75 Last.fm-Kandidaten aus `tag.getTopArtists`. Bis zu 3 MA-Kuenstler-Seeds erhalten ihren MA-Seedscore, je bis zu 50 Last.fm-Nachbarn aus `artist.getSimilar` die Last.fm-Aehnlichkeitsgewichtung. Beitraege addieren und normalisieren, nur die Genre-Kandidaten danach als Zufallspool vorbereiten; ohne Beitrag Score 0. Genregewicht nicht in den Kuenstlerscore einrechnen. Auf MA-Quellen aufloesen. |
| `global_top_tracks` | Track-Charts weltweit | Bis zu 50 bestplatzierte Last.fm-Tracks. | Nur Last.fm `chart.getTopTracks`; Last.fm-Reihenfolge erhalten und auf MA-Quellen aufloesen. Keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `de_top_tracks` | Track-Charts Deutschland | Bis zu 50 bestplatzierte Tracks unter deutschen Last.fm-Hoerern. | Nur Last.fm `geo.getTopTracks`, `country=Germany`; laut Last.fm-API-Dokumentation letzte Woche. Last.fm-Reihenfolge erhalten; keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_top_artists` | Kuenstler-Charts weltweit | Bis zu 25 weltweit beliebte Kuenstler. | Nur Last.fm `chart.getTopArtists`, bis zu 50 Kandidaten; Last.fm-Reihenfolge erhalten. Keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `de_top_artists` | Kuenstler-Charts Deutschland | Bis zu 25 in Deutschland beliebte Kuenstler. | Nur Last.fm `geo.getTopArtists`, `country=Germany`, bis zu 50 Kandidaten; Last.fm-Reihenfolge erhalten. Keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_popular_artist_albums` | Alben weltweit beliebter Kuenstler | Bis zu 35 Alben, keine Albumcharts. | Bis zu 10 bestplatzierte Last.fm-Kuenstler aus `chart.getTopArtists` bestimmen die Albumquellen. Das Ergebnismaximum gleichmaessig auf die Kuenstler verteilen und zur Albumquote aufrunden. Je Kuenstler bis zu 50 Alben aus `artist.getTopAlbums` abrufen, die besten `min(3 * Albumquote, 10)` zufaellig mischen und bis zur Quote auf MA-Quellen aufloesen; bei 35 Alben stoppen und die Auswahl mischen. Keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `de_popular_artist_albums` | Alben in Deutschland beliebter Kuenstler | Bis zu 35 Alben anhand des deutschen Kuenstler-Rankings. | Bis zu 10 bestplatzierte Last.fm-Kuenstler aus `geo.getTopArtists` mit `country=Germany` bestimmen die Albumquellen. Das Ergebnismaximum gleichmaessig auf die Kuenstler verteilen und zur Albumquote aufrunden. Je Kuenstler bis zu 50 Alben aus `artist.getTopAlbums` abrufen, die besten `min(3 * Albumquote, 10)` zufaellig mischen und bis zur Quote auf MA-Quellen aufloesen; bei 35 Alben stoppen und die Auswahl mischen. Keine persoenliche MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_genre_playlists` | Genre-Playlists entdecken | Bis zu 20 Playlists globaler Explore-Genres und Subgenres mit je bis zu 50 Tracks. | Bereinigte, eindeutige globale Last.fm-Tags aus `chart.getTopTags` auswaehlen; je Tag bis zu 150 Track-Kandidaten aus einer `tag.getTopTracks`-Abfrage als Zufallspool vorbereiten. Eindeutige Kandidaten mischen und bis zu 50 Tracks auf MA-Quellen aufloesen; fehlende Treffer durch weitere Kandidaten ersetzen. Keine Trackgrenze pro Kuenstler, persoenlichen MA-Seeds, MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_genre_albums_{genre_key}` | {Genre}: Alben entdecken | Bis zu 3 Reihen globaler Explore-Genres mit je bis zu 35 Alben. | Bis zu 3 bereinigte, eindeutige globale Last.fm-Genres in Rangfolge aus `chart.getTopTags` auswaehlen; je Genre bis zu 105 Last.fm-Alben aus `tag.getTopAlbums` als Zufallspool vorbereiten. Keine persoenlichen MA-Seeds, MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_genre_artists_{genre_key}` | {Genre}: Kuenstler entdecken | Bis zu 3 Reihen globaler Explore-Genres mit je bis zu 25 Kuenstlern. | Bis zu 3 bereinigte, eindeutige globale Last.fm-Genres in Rangfolge aus `chart.getTopTags` auswaehlen; je Genre bis zu 75 Last.fm-Kuenstler aus `tag.getTopArtists` als Zufallspool vorbereiten. Keine persoenlichen MA-Seeds, MA-History, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_artist_radio_playlists` | Kuenstler-Radios | Bis zu 15 Mehrkuenstler-Playlists mit je bis zu 50 Tracks, etwa Featuring Radiohead. | Bis zu 15 bestplatzierte Last.fm-Kuenstler aus `chart.getTopArtists` bestimmen die Playlists. Je Seed bis zu 50 Last.fm-Nachbarn aus `artist.getSimilar`; der Seed selbst erhaelt Score 1, Nachbarn ihren Match-Score. Daraus Trackquoten berechnen. Je benoetigtem Kuenstler bis zu 50 Tracks aus `artist.getTopTracks` abrufen, die besten `min(3 * Trackquote, 25)` zufaellig mischen und bis zur Quote auf MA-Quellen aufloesen; bei 50 Tracks stoppen und die Playlist mischen. Keine persoenlichen MA-Seeds, MA-Favoritenboni oder Rotationsausschluesse. |
| `global_mood_playlists` | Stimmungs-Playlists | Bis zu 10 Stimmungs-Playlists mit je bis zu 50 Tracks. | Feste Last.fm-Tags `chillout`, `ambient`, `energetic`, `melancholic`, `love`, `workout`, `chill`, `sad`, `focus`, `party`; je bis zu 150 Last.fm-Tracks aus `tag.getTopTracks` als Zufallspool vorbereiten; keine Trackgrenze pro Kuenstler. Keine persoenliche MA-History, MA-Favoritenboni, Genreprofil-Auswertung, Audioanalyse oder Deezer-Abfrage. |
| `global_decade_playlists` | Jahrzehnt-Playlists | Bis zu 5 Jahrzehnt-Playlists mit je bis zu 50 Tracks. | Feste Last.fm-Tags `1980s` bis `2020s`; je bis zu 150 Last.fm-Tracks aus `tag.getTopTracks` als Zufallspool vorbereiten; keine Trackgrenze pro Kuenstler. Keine persoenliche MA-History oder MA-Favoritenboni; Last.fm-Tag-Zugehoerigkeit garantiert kein exaktes Veroeffentlichungsjahr. |

