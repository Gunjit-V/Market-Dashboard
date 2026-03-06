from downloader.ohlcv import download_historical_data

if __name__ == "__main__":
    download_historical_data(
        instrument_types=["AMXIDX", "FUTIDX", "OPTIDX"],
        days=30
    )
