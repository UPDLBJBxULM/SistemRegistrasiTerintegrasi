import os
import io
import base64
import secrets
from PIL import Image
import google.generativeai as genai
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from datetime import datetime
from googleapiclient.http import MediaIoBaseUpload
from dotenv import load_dotenv

# Konfigurasi Aplikasi Flask
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64MB limit
print("MAX_CONTENT_LENGTH:", app.config.get('MAX_CONTENT_LENGTH')) # Cetak!

load_dotenv()

app.config["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY")
SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# Fungsi Ekstraksi Plat Nomor (Adaptasi dari kode legacy)
def extract_plate_number_gemini(image_path):
    try:
        genai.configure(api_key=app.config["GEMINI_API_KEY"])
        model = genai.GenerativeModel('gemini-2.0-flash') # Gunakan model vision untuk gambar, lebih akurat untuk gambar
        image_pil = Image.open(image_path)
        image_byte_stream = io.BytesIO()
        image_pil.save(image_byte_stream, format=image_pil.format)
        image_bytes = image_byte_stream.getvalue()

        image_parts = [
            {
                'mime_type': 'image/png' if image_path.lower().endswith(('.png')) else 'image/jpeg',
                'data': image_bytes
            },
        ]
        # Prompt yang lebih spesifik untuk ekstraksi plat nomor
        prompt_text = "Ekstrak plat nomor kendaraan dari gambar ini. Jika ada, berikan hasilnya HANYA plat nomor saja (contoh: AB 1234 CDE), tanpa teks atau simbol tambahan. Jika tidak ada plat nomor yang terlihat atau tidak dapat dikenali, jawab 'TIDAK DITEMUKAN'."

        request_content = [image_parts[0], prompt_text]

        response = model.generate_content(request_content)
        response.resolve()

        print("\n--- Response Teks dari Gemini API untuk Plat Nomor ---")
        print(response.text)
        print("--- Akhir Response Teks ---")

        teks_respon = response.text.strip()

        if "TIDAK DITEMUKAN" in teks_respon.upper():
            return None # Kembalikan None jika tidak ditemukan
        else:
            return teks_respon # Kembalikan plat nomor yang diekstrak

    except Exception as e:
        app.logger.error(f"Terjadi kesalahan saat memanggil Gemini API untuk plat nomor: {e}", exc_info=True)
        return None

# Fungsi untuk menyimpan data ke Google Spreadsheet
def append_to_sheet(spreadsheet_id, data):
    try:
        # Authenticate and authorize Google Sheets API
        creds = google.oauth2.service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file'] # Tambahkan scope drive
        )
        service = build('sheets', 'v4', credentials=creds)

        values = [data]
        body = {'values': values}
        range_ = "CheckIn!A1" # Ganti "Sheet1" dengan nama sheet Anda jika berbeda, pastikan sesuai dengan nama sheet anda

        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
        print(f"{result.get('updates').get('updatedCells')} cells appended.")
        return True

    except HttpError as error:
        app.logger.error(f"Terjadi kesalahan saat mengakses Google Spreadsheet: {error}")
        return False
    except Exception as e:
        app.logger.error(f"Kesalahan tak terduga saat menyimpan ke spreadsheet: {e}", exc_info=True)
        return False

# Fungsi untuk mengupload gambar ke Google Drive
def upload_to_drive(image_bytes, filename, folder_id):
    try:
        creds = google.oauth2.service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=['https://www.googleapis.com/auth/drive.file']
        )
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }

        media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype='image/png')  # Gunakan MediaIoBaseUpload

        file = service.files().create(
            body=file_metadata,
            media_body=media,  # Gunakan media sebagai objek MediaIoBaseUpload
            fields='id'
        ).execute()

        file_id = file.get('id')
        view_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"  # Membuat link viewable
        print(f"File ID: {file_id}")
        return view_link  # Kembalikan link viewable

    except HttpError as error:
        app.logger.error(f"Terjadi kesalahan saat mengupload ke Google Drive: {error}")
        return None
    except Exception as e:
        app.logger.error(f"Kesalahan tak terduga saat upload Google Drive: {e}", exc_info=True)
        return None


@app.route("/", methods=["GET", "POST"])
def index():
    return render_template("index.html")

@app.route("/extract_plate", methods=["POST"])
def extract_plate():
    try:
        image_data = request.form.get('image')
        if not image_data:
            return jsonify({'error': 'Tidak ada data gambar yang diterima.'}), 400

        # Jika terdapat koma, ambil bagian setelah koma (asumsi header ada),
        # jika tidak, gunakan seluruh string sebagai data base64 murni.
        if ',' in image_data:
            image_data = image_data.split(',')[1]

        image_bytes = base64.b64decode(image_data)
        
        # Buat file sementara untuk menyimpan gambar
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_image_path = os.path.join("tmp", "uploads", f"temp_image_{timestamp}.png")
        os.makedirs(os.path.dirname(temp_image_path), exist_ok=True)

        with open(temp_image_path, "wb") as fh:
            fh.write(image_bytes)

        # Ekstraksi plat nomor dengan Gemini API
        extracted_plate = extract_plate_number_gemini(temp_image_path)

        # Upload gambar ke Google Drive
        drive_filename = f"registrasi_kendaraan_{timestamp}.png"
        drive_url = upload_to_drive(image_bytes, drive_filename, DRIVE_FOLDER_ID)

        os.remove(temp_image_path)  # Hapus file sementara

        if extracted_plate and drive_url:
            return jsonify({'plateNumber': extracted_plate, 'imageUrl': drive_url})
        elif extracted_plate:
            return jsonify({'plateNumber': extracted_plate, 'imageUrl': None, 'warning': 'Gambar gagal diupload ke Google Drive, namun plat nomor berhasil diekstrak.'})
        elif drive_url:
            return jsonify({'plateNumber': None, 'imageUrl': drive_url, 'warning': 'Plat nomor gagal diekstrak, namun gambar berhasil diupload ke Google Drive.'})
        else:
            return jsonify({'error': 'Plat nomor dan gambar gagal diproses.'})

    except Exception as e:
        app.logger.error(f"Error saat memproses ekstraksi plat nomor dan upload gambar: {e}", exc_info=True)
        return jsonify({'error': 'Terjadi kesalahan server saat memproses gambar dan upload.'}), 500

# Route untuk registrasi kendaraan - MODIFIKASI URUTAN DATA SESUAI SPREADSHEET
@app.route("/register_vehicle", methods=["POST"])
def register_vehicle():
    plate_number = request.form.get('plateNumber')
    vehicle_type = request.form.get('vehicleType')
    purpose = request.form.get('purpose')
    image_url = request.form.get('imageUrl') # Dapatkan URL gambar dari form

    if not plate_number:
        return jsonify({'status': 'error', 'message': 'Plat Nomor wajib diisi!'}), 400

    # Urutan data sekarang: Foto Kendaraan (URL), Plat Nomor, Jenis Kendaraan, Tujuan
    registration_data = [image_url if image_url else "-",  # Foto Kendaraan (URL) - Kolom A
                         plate_number,                      # Plat Nomor Kendaraan - Kolom B
                         vehicle_type if vehicle_type else "-", # Jenis Kendaraan - Kolom C
                         purpose if purpose else "-"]        # Tujuan - Kolom D

    if append_to_sheet(SPREADSHEET_ID, registration_data):
        return jsonify({'status': 'success', 'message': 'Registrasi Kendaraan Berhasil!'})
    else:
        return jsonify({'status': 'error', 'message': 'Gagal menyimpan data registrasi ke spreadsheet. Silakan coba lagi.'}), 500

if __name__ == "__main__":
    app.run(debug=True, port=8090) # Jangan gunakan debug=True di produksi, tambahkan port=8090