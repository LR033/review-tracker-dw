# Platform icons

Small platform favicons shown next to the text badge on each review card
(`platform_badge()` in `dashboard/app.py`). They are embedded into the page as
base64 data URIs, so the dashboard has **no runtime dependency on external image
hosting**.

One PNG per platform, named by the platform slug used in `data/reviews.csv`:
`freetour`, `guruwalk`, `getyourguide`, `viator`, `tripadvisor`, `google`.

Source: each platform's own published favicon (fetched once, 48–96px). These are
third-party trademarks, used here nominatively to identify the source platform of
a review — not as an endorsement. To refresh or add one, drop a small square PNG
named `<slug>.png` in this folder; the badge picks it up automatically.
