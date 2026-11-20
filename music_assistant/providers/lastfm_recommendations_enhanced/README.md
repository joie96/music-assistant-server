# Last.fm Recommendations Enhanced

Persoenliche Empfehlungen aus Music Assistant (MA) und allgemeine Last.fm-Charts,
Genre-Auswahlen sowie dynamische Playlists. Die Wiedergabe erfolgt ueber MA;
Bibliothek, Favoriten und Hoerverlauf werden nicht veraendert.

## Voraussetzungen

- Ein eigener [Last.fm API-Key](https://www.last.fm/api/account/create).
  Kein mitgelieferter Key, Last.fm-Login, Benutzername oder API-Secret erforderlich.
- Einen aktivierten MA-Nutzer im Setup auswaehlen. Die Auswahl kann spaeter
  jederzeit in den Provider-Einstellungen unter "Music Assistant user for
  recommendations" (`recommendation_user_id`) geaendert werden.
  Seine History, Favoriten und verfuegbaren Musikquellen bilden die
  persoenliche Grundlage; die Reihen sind nur fuer diesen Nutzer sichtbar.
  Persoenliche Genre-Empfehlungen verwenden ausschliesslich Last.fm-Tags seiner bevorzugten
  Kuenstler, keine MA-Genrezuordnungen. Auch nicht synchronisierte Streaming-
  Kuenstler koennen beitragen, wenn MA ihre Credits im Hoerverlauf gespeichert hat.

## Limits und Hinweise

- Folder und Playlist-Tracks werden nach dem Start mit 20 Sekunden Verzoegerung
  und danach alle 24 Stunden im Hintergrund aufgebaut. Abrufe liefern den letzten
  vollstaendigen Stand ohne neue Abfragen; bis zum ersten Aufbau ist die Liste leer.
  Bei einem fehlgeschlagenen Refresh bleibt der vorherige Stand erhalten.
- Maximal 50 Kandidaten je Abfrage, keine Katalogdurchlaeufe. Persoenliche
  Vorschlaege verwenden hoechstens drei Ausgangspunkte; das Genreprofil wertet
  maximal zehn Kuenstler mit je fuenf bereinigten Tags aus. Die Aufloesung ist auf
  sechs Quellsuchen pro Folder- oder Playlist-Aufbau begrenzt. Nicht aufloesbare
  Medien entfallen, Reihen koennen daher kuerzer oder leer sein.
- Alle Enhanced-Instanzen teilen ein lokales Limit von fuenf Last.fm-Requests
  pro Sekunde. Last.fm kann strengere Limits setzen. Bei Rate Limits gilt ein
  gemeinsamer Cooldown von mindestens 60 Sekunden bzw. einem laengeren `Retry-After`.
- Oeffentliche Antworten werden im MA-Cache gespeichert: Charts sechs Stunden,
  Aehnlichkeit und Kuenstler-Tags sieben Tage, weitere Listen einen Tag.
  Last.fm-Cache-Vorgaben haben Vorrang; die Antwortnutzlast ist auf 32 MiB begrenzt.
  Vorbereitete persoenliche Ergebnisse bleiben an den ausgewaehlten MA-Nutzer gebunden.
- Rediscovery gleicht maximal 500 historische mit 500 aktuellen Tracks ab.
  Erreicht die aktuelle Rotation diese Grenze, bleibt die Reihe leer.
- Playlists sind dynamisch, nicht editierbar und enthalten hoechstens 50 Tracks.
  Sie werden nicht als dauerhafte MA-Playlists angelegt; Links koennen bei
  geaendertem Profil oder geaenderter Auswahl ungueltig werden.
- Last.fm-Rankings sind keine Verkaufscharts. Albumreihen sind abgeleitete
  Auswahlen; Mood- und Jahrzehnt-Tags garantieren weder Audioeigenschaften noch
  exakte Veroeffentlichungsjahre.
- Genregewichte kombinieren persoenliche Kuenstlergewichte mit pro Kuenstler
  normalisierten Last.fm-`count`-Werten; fehlende oder ungueltige Werte nutzen die
  Tag-Rangfolge. Nullgewichte entfallen. Bekannte Nicht-Genre-Tags werden entfernt,
  Synonyme zusammengefuehrt und Subgenres erhalten. Community-Tags sind trotzdem
  keine gepruefte Genre-Taxonomie und ihre Gewichte keine Wahrscheinlichkeiten.

Es gelten die [Last.fm-API-Nutzungsbedingungen](https://www.last.fm/api/tos),
insbesondere fuer Attribution und kommerzielle Nutzung. Last.fm liefert keine
Streamingrechte; Audio wird ausschliesslich ueber die eingerichteten MA-Quellen abgespielt.

## Folder

`monthly` bezeichnet die letzten 30 Tage. Die drei Toplisten nutzen ausschliesslich
diesen Zeitraum; die Ausgangspunkte fuer persoenliche Empfehlungen kombinieren
dagegen mehrere Zeitraeume, auch bei Folder-IDs mit dem Suffix `monthly`.

**Track-Seeds** kombinieren jeweils bis zu 50 MA-Top-Tracks aus `month`, `week`
und `all_time` mit bis zu 50 als Favorit markierten MA-Tracks des ausgewaehlten
Nutzers. **Kuenstler-Seeds** verwenden entsprechend MA-Top-Kuenstler und
Kuenstler-Favoriten. Das Gewicht pro MA-Identitaet ist
`Monats-Plays + 2 * Wochen-Plays + 0.1 * Allzeit-Plays + 5 bei Favorit`.
Die Zeitfenster ueberlappen absichtlich; aktuelle Plays wirken dadurch staerker.
Der Favoritenbonus ist ein Empfehlungsgewicht, keine zusaetzliche Wiedergabe.
Auch Favoriten ohne Plays koennen beitragen. Nur verfuegbare Ausgangspunkte
werden beruecksichtigt; fuer normale Empfehlungen bleiben die drei staerksten
eindeutigen Seeds, fuer das Genreprofil bis zu zehn Kuenstler.

Track-Favoriten fliessen nur in `user_recommendations` ein. Kuenstler-Favoriten
fliessen in Kuenstler-Seeds und darueber in persoenliche Kuenstler- und Album-
Empfehlungen sowie `user_genre_playlists_monthly`, `user_genre_artists_{genre_key}`
und `user_related_genre_playlists` ein. Die drei Explore-Genre-Familien verwenden keine
persoenlichen Seeds oder Favoriten. Track-Favoriten werden nicht als
Kuenstler-Favoriten umgedeutet; Album-Favoriten werden nirgends separat gewichtet.
Die Auswahl eines Favoriten legt keine neuen Bibliotheksobjekte an.

**Genreprofil** bedeutet hier ausschliesslich die Last.fm-Tags der Kuenstler-Seeds,
keine MA-Genrestatistik oder MA-Genrezuordnung. Je Kuenstler werden die ersten
20 `artist.getTopTags` geprueft und bis zu fuenf bereinigte, eindeutige Genre-Tags
behalten. Gueltige `count`-Werte werden relativ zum staerksten Tag gewichtet;
fehlende oder ungueltige Werte verwenden `1 / Listenrang`. Nullgewichte entfallen.
Diese Gewichte werden pro Kuenstler auf Summe eins normalisiert, mit dessen
persoenlichem Seed-Gewicht multipliziert und pro Genre ueber Kuenstler addiert.
Die drei staerksten Genres bestimmen die persoenlichen Genre-Reihen und Playlists;
die sechs Hauptprofil-Genres werden beim Ausschluss verwandter Genres beruecksichtigt.
Fuer verwandte Genres
wird je einem der drei staerksten Kuenstler ein Last.fm-Nachbar zugeordnet:
dessen Tags erben das Ausgangskuenstlergewicht. Bereits im Hauptprofil enthaltene
Genres entfallen; bis zu drei weitere bleiben. `{genre_key}` ist ein stabiler
Hash des normalisierten Tags, nicht dessen wechselnder Rang.

**Explore-Genres** stammen dagegen nur aus den globalen Last.fm-Top-Tags:
`chart.getTopTags` liefert eine Seite mit hoechstens 50 Tags. Bekannte Nicht-Genre-
Tags werden entfernt, Synonyme dedupliziert und die ersten bis zu 20 verbleibenden
Tags in Last.fm-Reihenfolge fuer Playlists, Album- und Kuenstlerreihen verwendet.
Keine persoenlichen Gewichte, kein Rotationsausschluss, kein Rueckfall auf
Lieblingsgenres bei leerer Quelle. Last.fm bietet keinen dokumentierten
Laender-Top-Tags-Endpunkt; die Geo-API liefert nur Top-Kuenstler und Top-Tracks.
Die globale Auswahl benoetigt daher keine Laenderableitung oder eigene Genreliste.
Folder- und Playlist-IDs tragen ihren Geltungsbereich als Praefix: `user_` fuer
persoenliche Auswahlen, `global_` fuer weltweite oder allgemein nutzerunabhaengige
Auswahlen und `de_` fuer Deutschland. Einzelne Explore-Playlists verwenden
`global_genre_playlist_{genre_key}`, verwandte persoenliche Genre-Playlists
`user_related_genre_playlist_{genre_key}`.

**Mood-Auswahl:** Die vier bestehenden Kategorien Relaxation (`chillout`),
Concentration (`ambient`), Energy (`energetic`) und Melancholy (`melancholic`)
bleiben erhalten. Zusaetzlich gibt es sechs an Deezer Flow orientierte Kategorien:
Love/Liebe (`love`), Motivation (`workout`), Chill (`chill`), Sad/Traurigkeit (`sad`),
Focus/Fokus (`focus`) und Party (`party`). Der neue Focus-Eintrag verwendet den
Schluessel `focused`, damit die bestehende Concentration-Playlist mit Schluessel
`focus` unveraendert bleibt. Insgesamt entstehen zehn feste Mood-Playlists;
deren Tracks werden weiterhin dynamisch aus Last.fm-Tags geladen. Das ist eine
eigene Tag-Zuordnung, keine Deezer-Flow-Anbindung oder identische Mood-Klassifikation.
Ein Deezer-Konto wird nicht benoetigt.

**Darstellung und Bilder:** 17 der 24 Familien liefern Tracks, Kuenstler oder
Alben direkt unter ihrer Sammlungsueberschrift, ohne zusaetzliche Ordnerkachel.
Ihre Originalbilder bleiben unveraendert. `user_recommended_album` zeigt das
Album samt Tracks direkt mit seinem Originalcover.

Die sieben Playlist-Bereiche sind `user_genre_playlists_monthly`,
`user_artist_playlists_monthly`, `global_genre_playlists`,
`global_artist_radio_playlists`, `user_related_genre_playlists`,
`global_mood_playlists` und `global_decade_playlists`. Diese enthalten die
einzelnen Playlists ohne weitere Zwischenordner. Jede Playlist bekommt eine
2x2-Collage aus bis zu vier unterschiedlichen Bildern ihrer vorbereiteten
Tracks, einschliesslich vorhandener Albumcover. Fehlende Kacheln erhalten
neutrale Platzhalter; fehlgeschlagene Bilder und Duplikate werden uebersprungen.
Pro Playlist werden hoechstens acht unterschiedliche Bildquellen versucht.

Wie bei der ListenBrainz-Recommendations-Vorlage liegt oben ein schwarzer
Titel-Einschub mit geschwungener Unterkante: links gross `Genre`, `Kuenstler`,
`Radio`, `Stimmung` oder `Jahrzehnt`, rechts kleiner der zugehoerige Genre-,
Kuenstler-, Mood- oder Jahrzehntname. Lange Namen werden passend zur verfuegbaren
Pixelbreite gekuerzt, nicht ueber andere Beschriftungen gelegt.

Die gemeinsame MA-Bildbeschaffung nutzt vorhandene Bildcaches. Die Collagen
werden nur beim taeglichen Refresh erzeugt, ausserhalb der Ereignisschleife
gerendert und atomar im instanzbezogenen MA-Bildcache gespeichert. Listen-,
Playlist- und Bildabrufe bauen keine Collagen neu. Die Bildaufloesung prueft
Nutzersichtbarkeit, Instanzzugehoerigkeit und Pfadsicherheit. Aktuelle und
vorherige Covergeneration bleiben erhalten; aeltere eigene Dateien werden
bereinigt. Ein fehlgeschlagener oder abgebrochener Build veroeffentlicht keine
unvollstaendige neue Sammlung.

**Ergebnisaufbereitung:** Last.fm-Kandidaten werden zuerst auf verfuegbare MA-
Bibliotheksobjekte, dann auf passende sichtbare Streaming-Quellen aufgeloest;
das ist keine Favoritengewichtung. Nicht aufloesbare Kandidaten entfallen.
`Rotation ausschliessen` bedeutet: bereits in den bis zu 500 MA-Top-Items des
jeweiligen Medientyps aus `month` enthaltene Identitaeten werden entfernt,
nicht nur nach hinten sortiert. Rediscovery verwendet abweichend nur vorhandene
lokale Track-Identitaeten und keine Streaming-Suche. Die Zahl konkreter Genre-
Reihen haengt von persoenlichen Tags bzw. globalen Explore-Tags ab; Mood- und
Jahrzehnt-Auswahlen bleiben fest. Sichtbarkeit und verfuegbare Wiedergabequellen
bleiben an den ausgewaehlten MA-Nutzer gebunden, auch fuer allgemeine Explore-Reihen.

| Folder-ID | Titel | Inhalt | Herleitung |
| --- | --- | --- | --- |
| `user_recommendations` | Track-Empfehlungen | Persoenlich empfohlene Tracks. | Drei Track-Seeds inklusive Track-Favoriten; je bis zu 20 `track.getSimilar`. Kandidatengewicht = Summe `Seed-Gewicht / Listenrang`; gemeinsame Treffer addieren, Seed-Namen entfernen und Track-Rotation ausschliessen. |
| `user_genre_playlists_monthly` | Deine Genre-Playlists | Playlists der drei staerksten persoenlichen Genres. | Genreprofil aus Kuenstler-Seeds inklusive Kuenstler-Favoriten; je Genre bis zu 50 `tag.getTopTracks`. Track-Rotation ausschliessen, maximal fuenf Tracks pro Kuenstler; keine zusaetzliche Favoritengewichtung der Tracks. |
| `user_top_artists_monthly` | Deine meistgehoerten Kuenstler | Kuenstler-Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=artist`, `period=month`, bis zu 50. Play-Rangfolge erhalten, unverfuegbare und doppelte Identitaeten entfernen; keine Favoritenboni oder Last.fm-Daten. |
| `user_top_tracks_monthly` | Deine meistgehoerten Tracks | Track-Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=track`, `period=month`, bis zu 50. Play-Rangfolge erhalten, unverfuegbare und doppelte Identitaeten entfernen; keine Favoritenboni oder Last.fm-Daten. |
| `user_artist_recommendations` | Kuenstler-Empfehlungen | Passende, bislang weniger gehoerte Kuenstler. | Drei Kuenstler-Seeds inklusive Kuenstler-Favoriten; je bis zu 20 `artist.getSimilar`. Kandidatengewicht = Summe `Seed-Gewicht / Listenrang`; gemeinsame Treffer addieren, Seed-Namen entfernen und Kuenstler-Rotation ausschliessen. |
| `user_artist_playlists_monthly` | Deine Kuenstler-Playlists | Dynamische Playlists wie Featuring Radiohead. | Je einer der drei Kuenstler-Seeds inklusive Kuenstler-Favoriten plus bis zu zwei `artist.getSimilar`-Nachbarn; je bis zu 20 `artist.getTopTracks`. Listen reihum mischen und deduplizieren, Track-Rotation ausschliessen; keine separate Einspeisung von Track-Favoriten. |
| `user_album_recommendations` | Album-Empfehlungen | Zum persoenlichen Geschmack passende Alben. | Drei Kuenstler-Seeds inklusive Kuenstler-Favoriten liefern je bis zu drei `artist.getSimilar`-Nachbarn. Staerksten Seed, danach Nachbarn, danach weitere Seeds priorisieren; hoechstens drei eindeutige Kuenstler. Je bis zu 20 `artist.getTopAlbums` reihum mischen, auf 50 begrenzen und Album-Rotation ausschliessen. Keine Album-Favoritenboni. |
| `user_top_albums_monthly` | Deine meistgehoerten Alben | Album-Ranking der letzten 30 Tage. | Nur MA-`statistics/top_items`, `media_type=album`, `period=month`, bis zu 50; beruht auf MA-Zuordnungen gehoerter Tracks zu Alben. Play-Rangfolge erhalten; keine Album-Favoritenboni oder Last.fm-Daten. |
| `user_rediscovery_tracks` | Lange nicht gehoert | Frueher haeufig gehoerte Tracks ausserhalb der aktuellen Rotation. | Ohne Favoriten oder Last.fm: bis zu 500 MA-Allzeit-Top-Tracks minus bis zu 500 Monats-Top-Tracks nach URI. Erste 50 verfuegbare historische Kandidaten in Allzeit-Rangfolge; nur lokal aufloesbare Track-Identitaeten behalten. Bei 500 Monats-Treffern leer. |
| `user_genre_artists_{genre_key}` | Kuenstler aus deinem Lieblingsgenre: {Genre} | Kuenstler eines persoenlichen Schwerpunktgenres. | Eines der drei staerksten Genres aus Kuenstler-Seeds inklusive Kuenstler-Favoriten; bis zu 50 `tag.getTopArtists`. Drei Kuenstler-Seeds geben eigenen Treffern ihr Seed-Gewicht, je bis zu 20 `artist.getSimilar`-Nachbarn `Seed-Gewicht / Listenrang`; Gewichte addieren, nur innerhalb der Genre-Kandidaten sortieren und Kuenstler-Rotation ausschliessen. |
| `global_top_tracks` | Track-Charts weltweit | Bis zu 50 bestplatzierte Last.fm-Tracks. | Nur `chart.getTopTracks`; Last.fm-Reihenfolge erhalten und auf MA-Quellen aufloesen. Keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `de_top_tracks` | Track-Charts Deutschland | Bis zu 50 bestplatzierte Tracks unter deutschen Last.fm-Hoerern. | Nur `geo.getTopTracks`, `country=Germany`; laut API-Dokumentation letzte Woche. Last.fm-Reihenfolge erhalten; keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `global_top_artists` | Kuenstler-Charts weltweit | Weltweit beliebte Kuenstler. | Nur `chart.getTopArtists`, bis zu 50; Last.fm-Reihenfolge erhalten. Keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `de_top_artists` | Kuenstler-Charts Deutschland | In Deutschland beliebte Kuenstler. | Nur `geo.getTopArtists`, `country=Germany`, bis zu 50; Last.fm-Reihenfolge erhalten. Keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `global_popular_artist_albums` | Alben weltweit beliebter Kuenstler | Allgemeine Albumauswahl, keine Albumcharts. | Drei Kuenstler aus `chart.getTopArtists`; je bis zu 20 `artist.getTopAlbums` reihum mischen und deduplizieren, insgesamt bis zu 50. Keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `de_popular_artist_albums` | Alben in Deutschland beliebter Kuenstler | Albumauswahl anhand des deutschen Kuenstler-Rankings. | Drei Kuenstler aus `geo.getTopArtists`, `country=Germany`; je bis zu 20 `artist.getTopAlbums` reihum mischen und deduplizieren, insgesamt bis zu 50. Keine persoenliche History, Favoritenboni oder Rotationsausschluesse. |
| `global_genre_playlists` | Genre-Playlists entdecken | Playlists globaler Explore-Genres und Subgenres. | Bis zu 20 bereinigte, eindeutige globale `chart.getTopTags`; je bis zu 50 `tag.getTopTracks`, maximal fuenf Tracks pro Kuenstler. Keine persoenlichen Seeds, History, Favoritenboni oder Rotationsausschluesse. |
| `global_genre_albums_{genre_key}` | {Genre}: Alben entdecken | Albumreihe eines globalen Explore-Genres. | Gleiche globale Tag-Auswahl wie `global_genre_playlists`; je bis zu 50 `tag.getTopAlbums` in Last.fm-Reihenfolge. Keine persoenlichen Seeds, History, Favoritenboni oder Rotationsausschluesse. |
| `global_genre_artists_{genre_key}` | {Genre}: Kuenstler entdecken | Kuenstlerreihe eines globalen Explore-Genres. | Gleiche globale Tag-Auswahl wie `global_genre_playlists`; je bis zu 50 `tag.getTopArtists` in Last.fm-Reihenfolge. Keine persoenlichen Seeds, History, Favoritenboni oder Rotationsausschluesse. |
| `global_artist_radio_playlists` | Kuenstler-Radios | Mehrkuenstler-Playlists wie Radiohead & aehnliche Kuenstler. | Drei Kuenstler aus `chart.getTopArtists`, je mit bis zu zwei `artist.getSimilar`-Nachbarn; je Kuenstler bis zu 20 `artist.getTopTracks` reihum mischen und deduplizieren. Keine persoenlichen Seeds, Favoritenboni oder Rotationsausschluesse. |
| `user_related_genre_playlists` | Verwandte Genre-Playlists | Genres nahe am persoenlichen Geschmack. | Last.fm-Tags je eines Nachbarn der drei staerksten Kuenstler-Seeds inklusive Kuenstler-Favoriten erben deren Seed-Gewicht. Hauptprofil-Genres entfernen, drei weitere Genres waehlen; je bis zu 50 `tag.getTopTracks`, Track-Rotation ausschliessen, maximal fuenf Tracks pro Kuenstler. |
| `global_mood_playlists` | Stimmungs-Playlists | Vier bestehende und sechs Deezer-Flow-orientierte Mood-Kategorien. | Feste Last.fm-Tags `chillout`, `ambient`, `energetic`, `melancholic`, `love`, `workout`, `chill`, `sad`, `focus`, `party`; je bis zu 50 `tag.getTopTracks`, maximal fuenf Tracks pro Kuenstler. Keine persoenliche History, Favoritenboni, Genreprofil-Auswertung, Audioanalyse oder Deezer-Abfrage. |
| `global_decade_playlists` | Jahrzehnt-Playlists | Etwa 80er, 90er und 2000er. | Feste Last.fm-Tags `1980s` bis `2020s`; je bis zu 50 `tag.getTopTracks`, maximal fuenf Tracks pro Kuenstler. Keine persoenliche History oder Favoritenboni; Tag-Zugehoerigkeit garantiert kein exaktes Veroeffentlichungsjahr. |
| `user_recommended_album` | Album fuer dich | Ein Album mit seinen aufloesbaren Tracks in einer Reihe. | Gleiche Kuenstler-Seeds und Kuenstlerauswahl wie `user_album_recommendations`, inklusive Kuenstler-Favoriten. Nur den ersten reihum gewaehlten Albumkandidaten aufloesen und bei Album-Rotation verwerfen; falls vorhanden bis zu 50 Album-Tracks aus `album.getInfo` anhaengen. Kein Ersatzalbum bei Ausschluss, keine Album-Favoritenboni. |

