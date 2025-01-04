from typing import BinaryIO


class FileInfo:
    file: BinaryIO
    filename: str

    def __init__(self, file: BinaryIO, filename: str):
        self.file = file
        self.filename = filename