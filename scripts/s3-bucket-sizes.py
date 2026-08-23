#!/usr/bin/env python3

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from aiobotocore.session import get_session

CONFIG_ENTRIES_PATH = Path("/config/.storage/core.config_entries")

DEEP_ARCHIVE_STORAGE_TYPES = {
    "DeepArchiveStorage",
    "DeepArchiveObjectOverhead",
}


def json_default(value):
    """Serialize datetime values in debug request/response output."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Return the S3 bucket sizes in AWS for an AWS region using CloudWatch and the S3 credentials used by the Home Assistant S3 cloud backup integration."
    )
    parser.add_argument(
        "region",
        help="AWS region, for example eu-west-2",
    )
    return parser.parse_args()


def load_aws_s3_credentials():
    """Read the AWS credentials from Home Assistant's AWS S3 integration configuration."""
    try:
        with CONFIG_ENTRIES_PATH.open("r", encoding="utf-8") as handle:
            storage = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unable to read {CONFIG_ENTRIES_PATH}: {exc}"
        ) from exc

    credential_pairs = set()

    for entry in storage.get("data", {}).get("entries", []):
        if entry.get("domain") != "aws_s3":
            continue

        data = entry.get("data", {})
        access_key_id = data.get("access_key_id")
        secret_access_key = data.get("secret_access_key")

        if access_key_id and secret_access_key:
            credential_pairs.add((access_key_id, secret_access_key))

    if not credential_pairs:
        raise RuntimeError(
            "No Home Assistant aws_s3 config entry with credentials found"
        )

    if len(credential_pairs) > 1:
        raise RuntimeError(
            "Multiple aws_s3 config entries with different credentials found; "
            "cannot choose one unambiguously"
        )

    return next(iter(credential_pairs))


async def get_latest_bucket_size_metrics(cloudwatch):
    """
    Retrieve all S3 BucketSizeBytes storage-type series with one SEARCH()
    expression and retain only the first (newest) returned datapoint from
    each series.

    Returns:
        bucket -> storage_type -> {timestamp, value}
    """
    now = dt.datetime.now(dt.timezone.utc)
    start_time = now - dt.timedelta(days=2)

    # BucketSizeBytes is published daily, but using a 60-second query period
    # avoids rebucketing it into arbitrary 24-hour periods. The series remains
    # sparse; normally only one datapoint exists per day.
    search_expression = (
        "SEARCH("
        "'{AWS/S3,BucketName,StorageType} MetricName=\"BucketSizeBytes\"',"
        "'Average',"
        "60"
        ")"
    )

    query = {
        "Id": "s3sizes",
        "Expression": search_expression,
        "Label": "${PROP('Dim.BucketName')}|${PROP('Dim.StorageType')}",
        "ReturnData": True,
    }

    values = {}
    next_token = None

    while True:
        request = {
            "MetricDataQueries": [query],
            "StartTime": start_time,
            "EndTime": now,
            "ScanBy": "TimestampDescending",
        }

        if next_token:
            request["NextToken"] = next_token

        response = await cloudwatch.get_metric_data(**request)

        for result in response.get("MetricDataResults", []):
            timestamps = result.get("Timestamps", [])
            metric_values = result.get("Values", [])

            if not timestamps or not metric_values:
                continue

            label = result.get("Label", "")
            if "|" not in label:
                raise RuntimeError(
                    f"Unexpected CloudWatch SEARCH result label: {label!r}"
                )

            bucket, storage_type = label.split("|", 1)

            # ScanBy=TimestampDescending means element 0 is the newest
            # datapoint for this returned time series.
            if storage_type not in values.setdefault(bucket, {}):
                values[bucket][storage_type] = {
                    "timestamp": timestamps[0],
                    "value": metric_values[0],
                }

        next_token = response.get("NextToken")
        if not next_token:
            break

    return values


def is_included_in_total(storage_type):
    """Exclude incomplete multipart-upload staging metrics from total_bytes."""
    return not storage_type.endswith("StagingStorage")


def build_output(values):
    """
    Build one result per bucket from the newest datapoint of each storage type.

    All storage types for a bucket are expected to have the same CloudWatch
    timestamp. If they do not, fail rather than silently combine data from
    different timestamps.

    deep_archive_bytes contains DeepArchiveStorage plus
    DeepArchiveObjectOverhead.

    DeepArchiveS3ObjectOverhead remains in other_bytes.

    raw_values contains the exact selected CloudWatch values by StorageType.
    """
    output = []

    for bucket in sorted(values):
        bucket_values = values[bucket]
        if not bucket_values:
            continue

        timestamps = {
            item["timestamp"]
            for item in bucket_values.values()
        }

        if len(timestamps) != 1:
            timestamp_details = ", ".join(
                f"{storage_type}={item['timestamp'].isoformat()}"
                for storage_type, item in sorted(bucket_values.items())
            )
            raise RuntimeError(
                f"Latest BucketSizeBytes datapoints for bucket {bucket!r} "
                f"do not share one timestamp: {timestamp_details}"
            )

        snapshot_timestamp = next(iter(timestamps))

        raw_values = {
            storage_type: round(item["value"])
            for storage_type, item in sorted(bucket_values.items())
        }

        total_bytes = sum(
            value
            for storage_type, value in raw_values.items()
            if is_included_in_total(storage_type)
        )

        deep_archive_bytes = sum(
            raw_values.get(storage_type, 0)
            for storage_type in DEEP_ARCHIVE_STORAGE_TYPES
        )

        other_bytes = total_bytes - deep_archive_bytes

        output.append(
            {
                "name": bucket,
                "snapshot_timestamp": snapshot_timestamp.isoformat(),
                "total_bytes": total_bytes,
                "deep_archive_bytes": deep_archive_bytes,
                "other_bytes": other_bytes,
                "raw_values": raw_values,
            }
        )

    return output


async def collect(region):
    access_key_id, secret_access_key = load_aws_s3_credentials()
    session = get_session()

    async with session.create_client(
        "cloudwatch",
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    ) as cloudwatch:
        values = await get_latest_bucket_size_metrics(cloudwatch)

    return build_output(values)


def main():
    args = parse_args()

    try:
        result = asyncio.run(collect(args.region))
    except Exception as exc:
        print(f"S3 CloudWatch collector failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, separators=(",", ":"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
