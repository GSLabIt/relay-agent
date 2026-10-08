"""Validation of the berth.tenantdb.* commands (no Docker needed)."""

import pytest

from agent.commands.tenantdb import TenantDbCommands, _ident, _slug


@pytest.mark.parametrize("slug", ["Bad Slug", "../x", "", "a;b", "A", "x" * 64])
def test_unsafe_slugs_are_refused(slug):
    with pytest.raises(ValueError):
        _slug({"slug": slug})


def test_good_slug_and_identifier():
    assert _slug({"slug": "acme-1"}) == "acme-1"
    assert _ident("odoo_acme_own", "owner") == "odoo_acme_own"


@pytest.mark.parametrize("value", ["a b", "a;drop", "a-b", "", None])
def test_identifiers_with_shell_or_sql_syntax_are_refused(value):
    with pytest.raises(ValueError):
        _ident(value, "database")


@pytest.mark.parametrize(
    "path",
    ["/etc", "/scratch/../etc", "/scratch/a/b", "/wal_archive/x", "scratch"],
)
def test_export_only_serves_scratch_and_the_wal_archive(path, tmp_path):
    commands = TenantDbCommands(object(), str(tmp_path))
    with pytest.raises(ValueError):
        commands.export({"slug": "acme", "path": path, "name": "x"})


def test_ensure_only_accepts_postgres_images(tmp_path):
    class _Docker:
        class networks:  # noqa: N801
            @staticmethod
            def get(_name):
                return object()

        class containers:  # noqa: N801
            @staticmethod
            def get(_name):
                import docker

                raise docker.errors.NotFound("x")

    commands = TenantDbCommands(_Docker(), str(tmp_path))
    with pytest.raises(ValueError):
        commands.ensure(
            {
                "slug": "acme",
                "image": "evil/image:1",
                "command": ["postgres"],
                "environment": {
                    "POSTGRES_USER": "odoo",
                    "POSTGRES_PASSWORD": "x",
                },
            }
        )
