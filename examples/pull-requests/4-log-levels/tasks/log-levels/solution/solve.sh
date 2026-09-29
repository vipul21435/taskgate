#!/bin/sh
# Reference solution: take the level column, count each distinct value, and
# print "LEVEL COUNT" sorted by level in byte order.
set -eu
mkdir -p output
cut -d ' ' -f 2 input/app.log | LC_ALL=C sort | uniq -c | awk '{ print $2, $1 }' > output/counts.txt
