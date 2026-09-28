"""Cover art for things the library does not hold: where it is, and at what size.

A catalogue row names its picture as an address on the catalogue's own image
server -- TMDb and TheTVDB behind Radarr and Sonarr, Amazon behind Audible,
Deezer and Apple behind buskarr -- and a client loads it from there directly,
the way a book's sample is played straight from Audible. Nothing is fetched or
stored here; this module only decides which address to hand on.

Two sizes, because a row and a summary want very different pictures. Measured
2026-09-28: TMDb's `original` poster is 2000x3000 and a megabyte, its `w154`
is 12 KB; TheTVDB's full poster is up to 450 KB against 65 KB for its `_t`
copy; Amazon's `_SL500_` cover is 54 KB against 10 KB at `_SL160_`. A page of
twenty-five search results drawn from full-size posters is several megabytes of
pictures a phone shows at forty-four points. Each catalogue spells its size in
the address, so the smaller copy is a rewrite rather than a second request.

Only https is passed on. Whatever this returns is fetched by somebody's phone
or browser, and a plain-http address there is a mixed-content failure at best.
"""
import re

#: TMDb names a size as a path segment. `w500` is sharp on a phone's summary
#: screen at about 80 KB; `w154` is plenty for a 44-point row at three pixels
#: to the point.
TMDB_FULL_SIZE = "w500"
TMDB_THUMBNAIL_SIZE = "w154"
_TMDB = re.compile(r"^(https://image\.tmdb\.org/t/p/)[^/]+(/[^/]+)$")

#: TheTVDB keeps a copy at half size beside every picture, named with `_t`.
#: The full one is kept for the summary: one picture at a time can afford it.
_TVDB = re.compile(r"^(https://artworks\.thetvdb\.com/banners/.+?)(_t)?(\.(?:jpe?g|png))$",
                   re.IGNORECASE)

#: Amazon's image server scales to whatever `_SL<n>_` asks for.
AMAZON_FULL = "._SL500_."
AMAZON_THUMBNAIL = "._SL160_."
_AMAZON = re.compile(r"\._SL\d+_\.")
_AMAZON_HOST = re.compile(r"^https://m\.media-amazon\.com/images/I/")

#: Deezer's CDN spells the square's side twice in the file name. 250 is its
#: own "medium" and the smallest size above a row's pixels.
DEEZER_FULL_SIDE = 500
DEEZER_THUMBNAIL_SIDE = 250
_DEEZER = re.compile(
    r"^(https://[a-z0-9-]+\.dzcdn\.net/images/(?:cover|artist)/[0-9a-f]+/)\d+x\d+(-.+)$")
#: What Deezer answers for an artist or album it has no picture of: the path
#: with an empty id, which serves a grey silhouette. That is not a picture of
#: anybody, and a blank square beside a row says nothing either.
_DEEZER_PLACEHOLDER = re.compile(r"/images/(?:cover|artist)//")

#: Apple's CDN scales to the `<w>x<h>bb` the last path segment names.
ITUNES_FULL_SIDE = 600
ITUNES_THUMBNAIL_SIDE = 160
_ITUNES = re.compile(
    r"^(https://[a-z0-9-]+\.mzstatic\.com/image/thumb/.+/)\d+x\d+(bb\.(?:jpe?g|png|webp))$")

#: Longer than any real catalogue address, and short enough that a field
#: somebody filled with rubbish cannot bloat a stored row.
MAX_URL_LENGTH = 2048


def https_url(value) -> str | None:
    """An address worth handing to a client, or None.

    https only, with a host, and of a sensible length. Anything else reads as
    no picture at all rather than as a broken one.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.startswith("https://") or len(url) > MAX_URL_LENGTH:
        return None
    if len(url) <= len("https://") or url[len("https://")] in "/?#":
        return None
    if any(ch.isspace() for ch in url):
        return None
    return url


def art(value) -> dict:
    """`imageUrl` and `thumbnailUrl` for one picture, or nothing at all.

    Spread into a hit or a row: `{**artwork.art(url), ...}`. Empty when there
    is no usable picture, so a row without one carries neither key rather than
    two nulls -- absent is what every client already reads as "none".

    Where the catalogue offers no smaller copy, both names carry the same
    address. A client can always use `thumbnailUrl` for a row without first
    asking whether there is one.
    """
    url = https_url(value)
    if url is None or _DEEZER_PLACEHOLDER.search(url):
        return {}
    full, thumbnail = _sized(url)
    return {"imageUrl": full, "thumbnailUrl": thumbnail}


def _sized(url: str) -> tuple[str, str]:
    """(full, thumbnail) addresses for one picture, by what its host understands."""
    if match := _TMDB.match(url):
        return (f"{match[1]}{TMDB_FULL_SIZE}{match[2]}",
                f"{match[1]}{TMDB_THUMBNAIL_SIZE}{match[2]}")
    if match := _TVDB.match(url):
        return f"{match[1]}{match[3]}", f"{match[1]}_t{match[3]}"
    if _AMAZON_HOST.match(url) and _AMAZON.search(url):
        return (_AMAZON.sub(AMAZON_FULL, url, count=1),
                _AMAZON.sub(AMAZON_THUMBNAIL, url, count=1))
    if match := _DEEZER.match(url):
        full, small = DEEZER_FULL_SIDE, DEEZER_THUMBNAIL_SIDE
        return (f"{match[1]}{full}x{full}{match[2]}",
                f"{match[1]}{small}x{small}{match[2]}")
    if match := _ITUNES.match(url):
        full, small = ITUNES_FULL_SIDE, ITUNES_THUMBNAIL_SIDE
        return (f"{match[1]}{full}x{full}{match[2]}",
                f"{match[1]}{small}x{small}{match[2]}")
    return url, url


def arr_poster(row: dict) -> str | None:
    """The poster on a Radarr or Sonarr lookup row.

    `images` first, because it says which picture is the poster; a lookup row
    also carries fan art and banners, and a banner is a wide strip that makes a
    poor cover. `remotePoster` is the same address, where `images` is missing.
    `url` on an image is the arr's own cache and needs its API key, so it is
    never handed on -- only the catalogue's `remoteUrl` is.
    """
    for image in row.get("images") or ():
        if isinstance(image, dict) and image.get("coverType") == "poster":
            if url := https_url(image.get("remoteUrl")):
                return url
    return https_url(row.get("remotePoster"))


def audible_cover(product: dict | None) -> str | None:
    """A book's cover from an Audible product or similarity row.

    `product_images` maps a pixel size to an address, as `{"500": url}`, and
    comes with the `media` response group, which both the product lookup and
    the similarity lookup already ask for. The largest wins; the size is put
    in the address afterwards anyway.
    """
    images = (product or {}).get("product_images")
    if not isinstance(images, dict):
        return None
    ranked = []
    for size, url in images.items():
        try:
            ranked.append((int(size), url))
        except (TypeError, ValueError):
            continue
    for _, url in sorted(ranked, reverse=True):
        if found := https_url(url):
            return found
    return None
