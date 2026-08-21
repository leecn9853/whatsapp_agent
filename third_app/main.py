import uvicorn


def main():
    uvicorn.run("thrid_app.server:app", host="0.0.0.0", port=8800, reload=True)


if __name__ == "__main__":
    main()
