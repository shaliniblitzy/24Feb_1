"""
Test data factory functions for the Flask Binary Repository Management System.

This package provides reusable test data generators organized by domain:
- repository_data: Repository configuration factories (Hosted, Proxy, Group × 7 formats)
- user_data: User entity factories with RBAC roles and authentication data
- artifact_data: Sample artifact generators for each supported repository format
- config_data: Configuration dictionaries for testing, development, and production environments

All factory functions follow the `make_*` naming convention and accept **overrides
for flexible customization. Uses factory-boy and Faker for deterministic data generation.
"""
