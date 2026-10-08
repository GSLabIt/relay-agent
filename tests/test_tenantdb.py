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


def test_export_refuses_dot_segments(tmp_path):
    commands = TenantDbCommands(object(), str(tmp_path))
    for path in ("/scratch/..", "/scratch/."):
        with pytest.raises(ValueError):
            commands.export({"slug": "acme", "path": path, "name": "x"})


def _ensure_params(**extra):
    return {
        "slug": "acme",
        "image": "postgres:16",
        "command": ["postgres"],
        "environment": {"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "x"},
        **extra,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"command": ["sh", "-c", "id"]},
        {"image": "alpine:3"},
        {"mem_limit": "64g"},
    ],
)
def test_ensure_refuses_unexpected_parameters_before_touching_docker(bad):
    class NoDocker:
        def __getattr__(self, name):
            raise AssertionError("docker must not be touched")

    with pytest.raises(ValueError):
        TenantDbCommands(NoDocker(), "/x").ensure(_ensure_params(**bad))


def test_failed_recreate_puts_the_previous_container_back(monkeypatch):
    from unittest.mock import MagicMock

    import docker

    previous = MagicMock()
    d = MagicMock()
    d.networks.get.return_value = MagicMock()
    d.containers.get.side_effect = [
        docker.errors.NotFound("old"),  # stale _old
        previous,  # the running container
        docker.errors.NotFound("new"),  # nothing under the name yet
        docker.errors.NotFound("new"),  # _put_back: nothing to remove
    ]
    d.containers.create.side_effect = RuntimeError("cannot create")
    commands = TenantDbCommands(d, "/x")
    with pytest.raises(RuntimeError):
        commands.ensure(_ensure_params(recreate=True))
    assert previous.rename.call_args_list[0].args[0] == "berth_pg_acme_old"
    assert previous.rename.call_args_list[-1].args[0] == "berth_pg_acme"
    previous.start.assert_called_once()


def test_cleanup_removes_only_the_given_operation(tmp_path):
    commands = TenantDbCommands(object(), str(tmp_path))
    base = tmp_path / "_tenantdb" / "acme"
    base.mkdir(parents=True)
    (base / "dump_aaaa1111.pgdump").write_bytes(b"1")
    (base / "dump_bbbb2222.pgdump").write_bytes(b"2")
    commands.cleanup(
        {"slug": "acme", "path": "_tenantdb/acme/dump_aaaa1111.pgdump"}
    )
    assert not (base / "dump_aaaa1111.pgdump").exists()
    assert (base / "dump_bbbb2222.pgdump").exists()
    commands.cleanup({"slug": "acme"})
    assert not base.exists()


@pytest.mark.parametrize(
    "path",
    [
        "_tenantdb/other/x.pgdump",
        "_tenantdb/acme/../other/x",
        "../etc/passwd",
        "/etc/passwd",
        "_tenantdb/acme/a/b/c",
    ],
)
def test_cleanup_refuses_paths_outside_the_slug(path, tmp_path):
    with pytest.raises(ValueError):
        TenantDbCommands(object(), str(tmp_path)).cleanup(
            {"slug": "acme", "path": path}
        )


def test_restore_refuses_an_unsafe_file_name(tmp_path):
    with pytest.raises(ValueError):
        TenantDbCommands(object(), str(tmp_path)).restore(
            {
                "slug": "acme",
                "owner": "o",
                "db_name": "d",
                "owner_password": "p",
                "file": "../../etc/passwd",
            }
        )
