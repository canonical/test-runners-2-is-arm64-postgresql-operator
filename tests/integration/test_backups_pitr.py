#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import uuid
from typing import Dict, Tuple

import boto3
import pytest as pytest
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_attempt, wait_exponential

from . import architecture
from .helpers import (
    CHARM_BASE,
    DATABASE_APP_NAME,
    MOVE_RESTORED_CLUSTER_TO_ANOTHER_BUCKET,
    construct_endpoint,
    db_connect,
    get_password,
    get_primary,
    get_unit_address,
)
from .juju_ import juju_major_version

CANNOT_RESTORE_PITR = "cannot restore PITR, juju debug-log for details"
S3_INTEGRATOR_APP_NAME = "s3-integrator"
if juju_major_version < 3:
    TLS_CERTIFICATES_APP_NAME = "tls-certificates-operator"
    if architecture.architecture == "arm64":
        TLS_CHANNEL = "legacy/edge"
    else:
        TLS_CHANNEL = "legacy/stable"
    TLS_CONFIG = {"generate-self-signed-certificates": "true", "ca-common-name": "Test CA"}
else:
    TLS_CERTIFICATES_APP_NAME = "self-signed-certificates"
    if architecture.architecture == "arm64":
        TLS_CHANNEL = "latest/edge"
    else:
        TLS_CHANNEL = "latest/stable"
    TLS_CONFIG = {"ca-common-name": "Test CA"}

logger = logging.getLogger(__name__)

AWS = "AWS"
GCP = "GCP"


@pytest.fixture(scope="module")
async def cloud_configs(github_secrets) -> None:
    # Define some configurations and credentials.
    configs = {
        AWS: {
            "endpoint": "https://s3.amazonaws.com",
            "bucket": "data-charms-testing",
            "path": f"/postgresql-vm/{uuid.uuid1()}",
            "region": "us-east-1",
        },
        GCP: {
            "endpoint": "https://storage.googleapis.com",
            "bucket": "data-charms-testing",
            "path": f"/postgresql-vm/{uuid.uuid1()}",
            "region": "",
        },
    }
    credentials = {
        AWS: {
            "access-key": github_secrets["AWS_ACCESS_KEY"],
            "secret-key": github_secrets["AWS_SECRET_KEY"],
        },
        GCP: {
            "access-key": github_secrets["GCP_ACCESS_KEY"],
            "secret-key": github_secrets["GCP_SECRET_KEY"],
        },
    }
    yield configs, credentials
    # Delete the previously created objects.
    logger.info("deleting the previously created backups")
    for cloud, config in configs.items():
        session = boto3.session.Session(
            aws_access_key_id=credentials[cloud]["access-key"],
            aws_secret_access_key=credentials[cloud]["secret-key"],
            region_name=config["region"],
        )
        s3 = session.resource(
            "s3", endpoint_url=construct_endpoint(config["endpoint"], config["region"])
        )
        bucket = s3.Bucket(config["bucket"])
        # GCS doesn't support batch delete operation, so delete the objects one by one.
        for bucket_object in bucket.objects.filter(Prefix=config["path"].lstrip("/")):
            bucket_object.delete()


async def pitr_backup_operations(
    ops_test: OpsTest,
    s3_integrator_app_name: str,
    tls_certificates_app_name: str,
    tls_config,
    tls_channel,
    credentials,
    cloud,
    config,
    charm,
) -> None:
    """Basic set of operations for PITR backup testing."""
    # Deploy S3 Integrator and TLS Certificates Operator.
    await ops_test.model.deploy(s3_integrator_app_name)
    await ops_test.model.deploy(tls_certificates_app_name, config=tls_config, channel=tls_channel)
    # Deploy and relate PostgreSQL to S3 integrator (one database app for each cloud for now
    # as archive_mode is disabled after restoring the backup) and to TLS Certificates Operator
    # (to be able to create backups from replicas).
    database_app_name = f"{DATABASE_APP_NAME}-{cloud.lower()}"
    await ops_test.model.deploy(
        charm,
        application_name=database_app_name,
        num_units=2,
        base=CHARM_BASE,
        config={"profile": "testing"},
    )

    await ops_test.model.relate(database_app_name, tls_certificates_app_name)
    async with ops_test.fast_forward(fast_interval="60s"):
        await ops_test.model.wait_for_idle(apps=[database_app_name], status="active", timeout=1000)
    await ops_test.model.relate(database_app_name, s3_integrator_app_name)

    # Configure and set access and secret keys.
    logger.info(f"configuring S3 integrator for {cloud}")
    await ops_test.model.applications[s3_integrator_app_name].set_config(config)
    action = await ops_test.model.units.get(f"{s3_integrator_app_name}/0").run_action(
        "sync-s3-credentials",
        **credentials,
    )
    await action.wait()
    async with ops_test.fast_forward(fast_interval="60s"):
        await ops_test.model.wait_for_idle(
            apps=[database_app_name, s3_integrator_app_name], status="active", timeout=1500
        )

    primary = await get_primary(ops_test, f"{database_app_name}/0")
    for unit in ops_test.model.applications[database_app_name].units:
        if unit.name != primary:
            replica = unit.name
            break

    # Write some data.
    password = await get_password(ops_test, primary)
    address = get_unit_address(ops_test, primary)
    logger.info("creating a table in the database")
    with db_connect(host=address, password=password) as connection:
        connection.autocommit = True
        connection.cursor().execute("CREATE TABLE IF NOT EXISTS backup_table_1 (test_column INT);")
    connection.close()

    # Run the "create backup" action.
    logger.info("creating a backup")
    action = await ops_test.model.units.get(replica).run_action("create-backup")
    await action.wait()
    backup_status = action.results.get("backup-status")
    assert backup_status, "backup hasn't succeeded"
    await ops_test.model.wait_for_idle(
        apps=[database_app_name, s3_integrator_app_name], status="active", timeout=1000
    )

    # Run the "list backups" action.
    logger.info("listing the available backups")
    action = await ops_test.model.units.get(replica).run_action("list-backups")
    await action.wait()
    backups = action.results.get("backups")
    assert backups, "backups not outputted"
    await ops_test.model.wait_for_idle(status="active", timeout=1000)

    # Write some data.
    logger.info("creating after-backup data in the database")
    with db_connect(host=address, password=password) as connection:
        connection.autocommit = True
        connection.cursor().execute(
            "INSERT INTO backup_table_1 (test_column) VALUES (1), (2), (3), (4), (5);"
        )
    connection.close()
    with db_connect(host=address, password=password) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_timestamp;")
        after_backup_ts = str(cursor.fetchone()[0])
    connection.close()
    with db_connect(host=address, password=password) as connection:
        connection.autocommit = True
        connection.cursor().execute("CREATE TABLE IF NOT EXISTS backup_table_2 (test_column INT);")
    connection.close()
    with db_connect(host=address, password=password) as connection:
        connection.autocommit = True
        connection.cursor().execute("SELECT pg_switch_wal();")
    connection.close()

    # Scale down to be able to restore.
    async with ops_test.fast_forward():
        await ops_test.model.destroy_unit(replica)
        await ops_test.model.block_until(
            lambda: len(ops_test.model.applications[database_app_name].units) == 1
        )

    for unit in ops_test.model.applications[database_app_name].units:
        remaining_unit = unit
        break

    most_recent_backup = backups.split("\n")[-1]
    backup_id = most_recent_backup.split()[0]
    # Wrong timestamp pointing to one year ahead
    wrong_ts = after_backup_ts.replace(after_backup_ts[:4], str(int(after_backup_ts[:4]) + 1), 1)

    # Run the "restore backup" action with bad PITR parameter.
    logger.info("restoring the backup with bad restore-to-time parameter")
    action = await remaining_unit.run_action(
        "restore", **{"backup-id": backup_id, "restore-to-time": "bad data"}
    )
    await action.wait()
    assert (
        action.status == "failed"
    ), "action must fail with bad restore-to-time parameter, but it succeeded"

    # Run the "restore backup" action with unreachable PITR parameter.
    logger.info("restoring the backup with unreachable restore-to-time parameter")
    action = await remaining_unit.run_action(
        "restore", **{"backup-id": backup_id, "restore-to-time": wrong_ts}
    )
    await action.wait()
    logger.info("waiting for the database charm to become blocked")
    async with ops_test.fast_forward():
        await ops_test.model.block_until(
            lambda: remaining_unit.workload_status_message == CANNOT_RESTORE_PITR,
            timeout=1000,
        )
    logger.info(
        "database charm become in blocked state, as supposed to be with unreachable PITR parameter"
    )

    # Run the "restore backup" action.
    for attempt in Retrying(
        stop=stop_after_attempt(10), wait=wait_exponential(multiplier=1, min=2, max=30)
    ):
        with attempt:
            logger.info("restoring the backup")
            action = await remaining_unit.run_action(
                "restore", **{"backup-id": backup_id, "restore-to-time": after_backup_ts}
            )
            await action.wait()
            restore_status = action.results.get("restore-status")
            assert restore_status, "restore hasn't succeeded"

    # Wait for the restore to complete.
    async with ops_test.fast_forward():
        await ops_test.model.block_until(
            lambda: remaining_unit.workload_status_message
            == MOVE_RESTORED_CLUSTER_TO_ANOTHER_BUCKET,
            timeout=1000,
        )

    # Check that the backup was correctly restored.
    primary = await get_primary(ops_test, remaining_unit.name)
    address = get_unit_address(ops_test, primary)
    logger.info("checking that the backup was correctly restored")
    with db_connect(host=address, password=password) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'backup_table_1');"
        )
        assert cursor.fetchone()[
            0
        ], "backup wasn't correctly restored: table 'backup_table_1' doesn't exist"
        cursor.execute("SELECT COUNT(1) FROM backup_table_1;")
        assert (
            int(cursor.fetchone()[0]) == 5
        ), "backup wasn't correctly restored: table 'backup_table_1' doesn't have 5 rows"
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'backup_table_2');"
        )
        assert not cursor.fetchone()[
            0
        ], "backup wasn't correctly restored: table 'backup_table_2' exists"
    connection.close()

    # Remove the database app.
    await ops_test.model.remove_application(database_app_name, block_until_done=True)
    # Remove the TLS operator.
    await ops_test.model.remove_application(tls_certificates_app_name, block_until_done=True)


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_pitr_backup_aws(ops_test: OpsTest, cloud_configs: Tuple[Dict, Dict], charm) -> None:
    """Build, deploy two units of PostgreSQL and do backup in AWS. Then, write new data into DB, switch WAL file and test point-in-time-recovery restore action."""
    config = cloud_configs[0][AWS]
    credentials = cloud_configs[1][AWS]

    await pitr_backup_operations(
        ops_test,
        S3_INTEGRATOR_APP_NAME,
        TLS_CERTIFICATES_APP_NAME,
        TLS_CONFIG,
        TLS_CHANNEL,
        credentials,
        AWS,
        config,
        charm,
    )


@pytest.mark.group(2)
@pytest.mark.abort_on_fail
async def test_pitr_backup_gcp(ops_test: OpsTest, cloud_configs: Tuple[Dict, Dict], charm) -> None:
    """Build, deploy two units of PostgreSQL and do backup in GCP. Then, write new data into DB, switch WAL file and test point-in-time-recovery restore action."""
    config = cloud_configs[0][GCP]
    credentials = cloud_configs[1][GCP]

    await pitr_backup_operations(
        ops_test,
        S3_INTEGRATOR_APP_NAME,
        TLS_CERTIFICATES_APP_NAME,
        TLS_CONFIG,
        TLS_CHANNEL,
        credentials,
        GCP,
        config,
        charm,
    )
