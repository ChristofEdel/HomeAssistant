# History Import Custom Integration

## Integration/action

Custom integration domain:

```text
history_import
```

Action:

```text
history_import.import
```

The action is available in Home Assistant’s Actions UI and has the following parameters:

| Parameter | Value                              | Default  |
|-----------|------------------------------------|----------|
| Entity    | sensor entity ID                   | -        |
| Time zone | `local` / `UTC`                    | `UTC`    |
| File      | path to CSV file                   | -        |
| States    | `append` / `overwrite` / `replace` | `append` |

At this stage, only entities with state_class: measurement are supported.

## Input File Format

The input has to be presented in a CSV file with two columns:

`
timestamp,value
`

The file is interpreted as follows:

- First column: timestamp
- Second column: numeric sensor value
- Any additional columns are ignored
- A header row is optional and, if present, it is ignored
- Blank lines are ignored
- At least one valid data row must be present
- lines must be in ascending time order

## Timestamp formats

The following timestamp formats are accepted:

|  |  | 
|--|--|
| `1724934600`<br/>`1724934600.123456`                              | UNIX Timestamp - always UTC   |
| `2026-08-29 14:30:00`<br/>`2026-08-29 14:30:00.123456`<br/>`2026-08-29T14:30:00`<br/>`2026-08-29T14:30:00.123456` | Date and time, local or UTC, as specified by the 'Time Zone' parameter |
| `2026-08-29T14:30:00Z`<br/>`2026-08-29T14:30:00.123456Z`          | Date and time, UTC                               |
| `2026-08-29T14:30:00+01:00`<br/>`2026-08-29T14:30:00.123456+01:00`| Date and time with explicit time zone offset |

Fractional seconds are optional. Only six fractional digits (up to microsecond precision) are stored.

Ambiguous or nonexistent local times caused by DST transitions are invalid.

## Validation

The **entire file is validated before any database changes are made**.

Validation includes:

- entity exists, and is a sensor with `state_class: measurement`;
- every data row has at least two columns;
- every timestamp is valid;
- every value is a finite numeric value;
- timestamps are strictly increasing (duplicate timestamps are invalid)

If validation fails, the action terminates with an error identifying the CSV line where the problem occurred.

## State import modes

| Mode | Explanation |
|------|------------|
|`append`| Import only states which are before the oldest existing Recorder state for that entity|
|`overwrite`| Import all states. Keep the original states which occurred before the first entry in the import|
|`replace`| Import all states, and remove the pre-existing states entirely |


## Statistics

After a successful state import, statistics (both short-term and long-term) affected by the operation are rebuilt from the resulting Recorder states.

For a `measurement` entity this means mean, min and max values for each bucket.

All buckets affected by the import are recalculated. 

The regenerated statistics follow Home Assistant’s normal calculation semantics, including the appropriate time-weighted calculation of the mean value in each bucket.
