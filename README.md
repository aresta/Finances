# Finances

Personal investment portfolio tracker with current and historical prices from Yahoo Finance.

## Installation

1. Install Python: https://www.python.org/downloads/
2. Install the application:
```bash
pip install https://github.com/aresta/Finances/raw/main/dist/finances-0.5.0-py3-none-any.whl
```
Note: If you use Python for other projects, better install the application in a separated enviroment (venv)


## Run

```bash
finances
```

Or:

```bash
python -m finances
```

To use a specific config file:

```bash
finances myconfig.conf
```

On first run, default `finances.conf` and `orders.csv` templates are created in the working directory — edit them to match your broker's CSV format.


## Configuration

`finances.conf` (TOML) specifies the orders file, CSV column mapping, date format, and cache settings. See the bundled defaults or `finances.conf` for examples.

In the repo there are two config files for **Indexa** and **MyInvestor**:

- finances_indexa.conf
- finances_myinvestor.conf

They should work out of the box (*almost*) with the cvs exported from their respectives web sites.  

*Note*: **MyInvestor** (dirty) cvs file needs two adjustments:
- For some obscure reason they decided to use different decimal separators in each column: '**.**' and '**,**' So harmonize it.
- They also forgot to mention in file if the operations  are 'buy' or 'sell'. So, check which ones are 'sell' and switch them to negative. Or add a proper column with: buy / refund


### Config example

```toml
[general]
title = "Portfolio Test"

[input]
orders_file = "ordres_test.csv"

[input.csv]
### Default columns
date    = 0     # Order date
isin    = 1     # ISIN (International Securities Identification Number)
amount  = 2     # Estimated total cost
shares  = 3     # Num of shares
subsc   = 4     # Subscription / refund (optional)

### Format
date_format = "%d/%m/%Y"
delimiter = ","
decimal_separator = "."
```

### Orders example

```csv
Date,ISIN,Total Cost,Num Shares,Subsc
03/03/2026,IE0007471471,30,0.329,Subscription
03/03/2026,IE0007471471,300,3.294,Subscription
03/03/2026,IE0007472990,300,1.380,Subscription
03/03/2026,IE00BYWYCC39,50,2.855,Subscription
03/03/2026,IE00B42W4L06,50,0.126,Subscription
03/03/2026,IE00B03HCZ61,4500,80.633,Subscription
03/03/2026,IE00BD0NCM55,4500,176.388,refund
03/03/2025,IE00BFPM9P35,209.78,0.879,refund
03/03/2025,IE0007471471,300,3.365,Subscription
```



## License

GPL-3.0