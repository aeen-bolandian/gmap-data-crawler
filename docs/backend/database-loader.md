# Database Loader

Developer: aeen
Date: 2026-10-20

## Overview
Read-only SQLite loader with integrity pre-check.

## Components
- `CafeRecord` dataclass
- `DatabaseLoader` context manager

## Notes
Opens DB in `?mode=ro` to prevent accidental writes.
