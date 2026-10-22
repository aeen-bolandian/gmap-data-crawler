"""
gmaps-review-crawler
====================

A modular, fault-tolerant pipeline for extracting Google Maps reviews
at scale using geospatial metadata.

Modules
-------
- db_loader : Secure SQLite data access
- resolver  : URL & search query builder (Phase 1)
- scraper   : Async Playwright review extractor (Phase 2)
- exporter  : Append-only CSV streaming
"""

__version__ = "1.0.0"
__author__ = "Data Engineering Team"
__license__ = "MIT"