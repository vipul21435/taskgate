# Log lines per level

The file `input/app.log` in the working directory holds one log record per
line: a timestamp, a single space, a level name in upper case (such as `INFO`
or `ERROR`), a single space, and a free-text message.

Count the records of each level and write the counts to `output/counts.txt`:
one line per level that occurs, as the level name, a single space and the
count in base 10, with the lines sorted by level name in byte order. End every
line, including the last, with a single `\n`. Create the `output/` directory if
it does not exist.
