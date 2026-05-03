# Curie

Curie is a **Calibre plugin** that generates spoiler-free hints of characters and places and injects these as footnotes into your EPUB:s.

## How it works

First Curie fetches all that it can about the book's plot from the web using Claude. This is then cross-referenced with the EPUB itself to make sure descriptions are correct, and at the same time removing spoilers. Being the nature of AI, this fetching and massaging of data is non-deterministic. A JSON file is saved in the Calibres books folder with the results.

The hints themselves (technically footnotes) are then injected into the EPUB. They are injected into the mention of it **after** the character or location has been described. This is processed locally. In other words, Claude doesn't change the EPUB itself (deterministic behaviour). This is good, as the injected hints added to the book can then be easily removed and modified.

## Why Curie?

Reading books and getting immersed in a story is pure magic. Forgetting names and having to backtrack breaks that. This can be especially true when reading books that are not in your native tongue and have character names you can't really "taste". I'm looking at you, Dostoevsky.

## API Costs

Using *Claude Haiku*, the average processing cost of an averaged length novel is around 0.30$. That includes both places and characters. Now – using *Sonnet* is costlier (around 3x), but from my experience Haiku deliveries quality summaries without any spoilers.

## Features

* Summarizes characters and/or locations in books (spoiler free!)

* Catches nicknames of characters and maps them correctly

* Choose density of hints (Every mention, every 10 paragraphs, once per chapter)

* Supports KOReader and Nickel

## KOReader vs. Nickel

### KOReader

* Allows a richer styling of pop-ups

* Needs some settings changed to allow the hints to show up as pop-ups

### Nickel

* Requires **KEPUB** format to be able display hints as pop-ups

* No formatting (CSS) is allowed in the pop-up, only shows raw text

## Roadmap

* [ ] Descriptions of character or places gets updated chapter-by-chapter as the story unfolds (how to not make this madly expensive API-wise?)
* [ ] Inspect generated data inside the plugin GUI. Expected behaviour: Click a button inside the plugin GUI to see a visual representation of characters and/or places
* [ ] Add map images for actual places, on country or world map (only feasible on KOReader)
  * [ ] Interactive map for exploring places mentioned in novel
* [ ] Ability to mark characters or places as "known" - requires Curie to be a plugin in KOReader
* [ ] Ability to toggle or highlight the hints upon user interaction (otherwise the hints should be hidden) - requires Curie to be a plugin in KOReader
