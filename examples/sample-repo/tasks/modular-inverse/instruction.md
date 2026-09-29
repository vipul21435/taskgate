# Modular inverses

The file `input/pairs.txt` in the working directory holds one pair of integers
`a m` per line, separated by a single space, with `0 <= a` and `m >= 2`.

For each line, in order, write one line to `output/inverses.txt`:

- the modular inverse of `a` modulo `m`, meaning the unique integer `x` with
  `0 <= x < m` and `(a * x) % m == 1`, written in base 10 without leading zeros;
- or the word `none` when no such `x` exists (when `a` and `m` share a factor).

End every line, including the last, with a single `\n`. Create the `output/`
directory if it does not exist.
