# Bundled fonts

The page loads nothing from the internet, so its three fonts ship here (batch 3, D-138). Each file is the upstream `.woff2`, unchanged apart from the file name, with its SIL Open Font License beside it.

| Family | Used for | File | Upstream file | From | SHA-256 |
|---|---|---|---|---|---|
| Bricolage Grotesque | headings (500, 650, 750) | `bricolage-grotesque/BricolageGrotesque-Variable.woff2` | `fonts/webfonts/BricolageGrotesque[opsz,wdth,wght].woff2` | [ateliertriay/bricolage](https://github.com/ateliertriay/bricolage), commit `84745e5` | `b51a8ebd169637e47cb7db430431ab3e122d2f09b03ee2a03ea06f4cb46f1a8e` |
| Instrument Sans | text (400, 500, 600) | `instrument-sans/InstrumentSans-Variable.woff2` | `fonts/webfonts/InstrumentSans[wdth,wght].woff2` | [Instrument/instrument-sans](https://github.com/Instrument/instrument-sans), commit `7fa2230` | `aa72922aafcc0dc18f36ec1d805b0212057dabe8b9d5b8b57f67035aea1b826d` |
| JetBrains Mono | console, addresses (400) | `jetbrains-mono/JetBrainsMono-Regular.woff2` | `fonts/webfonts/JetBrainsMono-Regular.woff2` | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) release v2.304 | `a9cb1cd82332b23a47e3a1239d25d13c86d16c4220695e34b243effa999f45f2` |
| JetBrains Mono | console, addresses (500) | `jetbrains-mono/JetBrainsMono-Medium.woff2` | `fonts/webfonts/JetBrainsMono-Medium.woff2` | same release | `086c48dfbea9ddaff1320f7e09399b8e2924e88ce67453721255db3bdbb5a353` |

The variable files are full character sets, not a Latin subset: subsetting would make a Modified Version under the OFL. About 480 KB together; the browser keeps them (a 304 on later visits).
