"""The audiobook engine: what to recommend, and how a book is asked for.

A subpackage rather than modules beside the others because none of it
generalises. An Audible similarity graph and a TF-IDF model over blurbs have
no analogue in film or television, where the ranking evidence is Jellyfin's
own metadata and the catalogue is whatever Radarr and Sonarr proxy.

What *is* shared lives one level up and is imported from there: the config,
the Jellyfin client, the request ledger, and the Listenarr transport that sits
beside Radarr's and Sonarr's.
"""
