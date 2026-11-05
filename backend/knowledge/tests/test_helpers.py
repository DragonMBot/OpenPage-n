from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


def make_pdf():
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    )
    content = bytearray(header)
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode())
        content.extend(body)
        content.extend(b"\nendobj\n")

    xref_offset = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(content)


def make_epub(
    *,
    metadata=True,
    cover=b"\xff\xd8\xff\xe0test-cover",
    container=None,
    extra_entries=None,
):
    metadata_xml = ""
    if metadata:
        metadata_xml = """
            <dc:title>Книга из EPUB</dc:title>
            <dc:creator>Автор EPUB</dc:creator>
            <dc:language>ru</dc:language>
            <dc:publisher>Издательство</dc:publisher>
            <dc:date>2024-05-10</dc:date>
            <meta name="cover" content="cover-image" />
        """
    cover_item = (
        '<item id="cover-image" href="images/cover.jpg" '
        'media-type="image/jpeg" properties="cover-image" />'
        if cover is not None
        else ""
    )
    opf = f"""<?xml version="1.0"?>
        <package xmlns="http://www.idpf.org/2007/opf"
                 xmlns:dc="http://purl.org/dc/elements/1.1/">
          <metadata>{metadata_xml}</metadata>
          <manifest>{cover_item}</manifest>
        </package>
    """
    container = (
        container
        or """<?xml version="1.0"?>
        <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles><rootfile full-path="OEBPS/content.opf" /></rootfiles>
        </container>
    """
    )

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            container,
            compress_type=ZIP_DEFLATED,
        )
        archive.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
        if cover is not None:
            archive.writestr("OEBPS/images/cover.jpg", cover)
        for name, content in extra_entries or ():
            archive.writestr(name, content)
    return output.getvalue()
