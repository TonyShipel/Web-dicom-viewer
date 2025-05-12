import os
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
import pydicom
import numpy as np
from PIL import Image
import base64
from io import BytesIO
from werkzeug.utils import secure_filename
import glob
import shutil
import uuid

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024  # 16GB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PERMANENT_FOLDER'] = 'permanent_uploads'

# Создаем папки для загрузок, если их нет
for folder in [app.config['UPLOAD_FOLDER'], app.config['PERMANENT_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Увеличиваем размер буфера для больших файлов
app.config['MAX_BUFFER_SIZE'] = 16 * 1024 * 1024 * 1024  # 16GB buffer size

def scan_dicom_folder(folder_path):
    """Сканирует папку и подпапки на наличие DICOM файлов"""
    dicom_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith('.dcm'):
                dicom_files.append(os.path.join(root, file))
    return sorted(dicom_files)

def process_dicom_file(file_data):
    """Обрабатывает DICOM файл из байтового потока."""
    try:
        # Читаем DICOM из байтового потока
        dataset = pydicom.dcmread(BytesIO(file_data))
        
        # Получаем данные изображения
        pixel_array = dataset.pixel_array
        
        # Рассчитываем оптимальные значения window/level из данных изображения
        pixel_min = float(pixel_array.min())
        pixel_max = float(pixel_array.max())
        dynamic_range = pixel_max - pixel_min
        
        # Устанавливаем window/level на основе данных изображения
        window_width = dynamic_range
        window_center = pixel_min + dynamic_range / 2
        
        # Если значения слишком большие или маленькие, корректируем их
        window_width = max(1, min(4096, window_width))
        window_center = max(-1024, min(1024, window_center))
        
        # Рассчитываем оптимальный начальный зум
        image_width = pixel_array.shape[1]
        image_height = pixel_array.shape[0]
        
        # Извлекаем метаданные
        metadata = {
            'PatientName': str(getattr(dataset, 'PatientName', '')),
            'PatientID': str(getattr(dataset, 'PatientID', '')),
            'StudyDate': str(getattr(dataset, 'StudyDate', '')),
            'Modality': str(getattr(dataset, 'Modality', '')),
            'SeriesDescription': str(getattr(dataset, 'SeriesDescription', '')),
            'WindowWidth': window_width,
            'WindowCenter': window_center,
            'ImageWidth': image_width,
            'ImageHeight': image_height
        }
        
        print(f"Image dimensions: {image_width}x{image_height}")
        print(f"Pixel range: min={pixel_min}, max={pixel_max}")
        print(f"Calculated WindowWidth: {window_width}, WindowCenter: {window_center}")
        
        # Применяем window/level
        min_value = window_center - window_width / 2
        max_value = window_center + window_width / 2
        
        # Нормализуем значения в диапазон 0-255 с учетом window/level
        normalized_array = np.clip(pixel_array, min_value, max_value)
        normalized_array = ((normalized_array - min_value) / (max_value - min_value) * 255.0)
        normalized_array = np.clip(normalized_array, 0, 255).astype(np.uint8)
        
        # Конвертируем в изображение
        image = Image.fromarray(normalized_array)
        
        # Сохраняем в буфер
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return image_base64, metadata
        
    except Exception as e:
        print(f"Error processing DICOM: {str(e)}")
        raise

def apply_window_level(image, window_center, window_width):
    min_value = window_center - window_width // 2
    max_value = window_center + window_width // 2
    windowed = np.clip(image, min_value, max_value)
    windowed = ((windowed - min_value) / (max_value - min_value) * 255).astype(np.uint8)
    return windowed

def array_to_base64(array):
    img = Image.fromarray(array)
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/cleanup', methods=['POST'])
def cleanup():
    try:
        data = request.json
        if not data or 'uploadId' not in data:
            return jsonify({'error': 'No upload ID provided'}), 400
            
        upload_id = data['uploadId']
        folder_path = os.path.join(app.config['PERMANENT_FOLDER'], upload_id)
        
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)
            app.logger.info(f'Cleaned up folder: {folder_path}')
            return jsonify({'message': 'Files cleaned up successfully'})
        else:
            return jsonify({'message': 'Folder already removed'})
            
    except Exception as e:
        app.logger.error(f"Error cleaning up files: {str(e)}", exc_info=True)
        return jsonify({'error': f'Error cleaning up files: {str(e)}'}), 500

@app.route('/scan_folder', methods=['POST'])
def scan_folder():
    try:
        app.logger.info('Received folder upload request')
        
        # Если есть предыдущая загрузка, очищаем её
        if 'currentUploadId' in request.form:
            try:
                old_folder = os.path.join(app.config['PERMANENT_FOLDER'], request.form['currentUploadId'])
                if os.path.exists(old_folder):
                    shutil.rmtree(old_folder)
                    app.logger.info(f'Cleaned up previous upload: {old_folder}')
            except Exception as e:
                app.logger.warning(f'Error cleaning up previous upload: {str(e)}')
        
        if 'folder' not in request.files:
            app.logger.error('No folder in request.files')
            return jsonify({'error': 'No folder selected'}), 400
        
        # Создаем уникальную папку для этой загрузки
        upload_id = str(uuid.uuid4())
        permanent_folder = os.path.join(app.config['PERMANENT_FOLDER'], upload_id)
        os.makedirs(permanent_folder)
        app.logger.info(f'Created permanent folder: {permanent_folder}')
        
        # Сохраняем все файлы из запроса
        uploaded_files = request.files.getlist('folder')
        app.logger.info(f'Number of uploaded files: {len(uploaded_files)}')
        
        if not uploaded_files:
            app.logger.error('No files in uploaded_files')
            return jsonify({'error': 'No files uploaded'}), 400
            
        dicom_files = []
        for file in uploaded_files:
            app.logger.info(f'Processing file: {file.filename}')
            if file.filename.lower().endswith('.dcm'):
                filename = secure_filename(file.filename)
                filepath = os.path.join(permanent_folder, filename)
                file.save(filepath)
                app.logger.info(f'Saved DICOM file: {filepath}')
                dicom_files.append(filepath)
        
        app.logger.info(f'Found {len(dicom_files)} DICOM files')
        if not dicom_files:
            app.logger.error('No DICOM files found')
            return jsonify({'error': 'No DICOM files found in the folder'}), 400
        
        # Сортируем файлы по имени для правильного порядка
        dicom_files.sort()
        
        # Обрабатываем первый файл для получения метаданных
        app.logger.info('Processing first DICOM file')
        img_str, metadata = process_dicom_file(dicom_files[0])
        
        # Получаем список всех файлов с их метаданными
        files_info = []
        total_files = len(dicom_files)
        for i, file_path in enumerate(dicom_files):
            img_str, file_metadata = process_dicom_file(file_path)
            files_info.append({
                'path': file_path,
                'instanceNumber': file_metadata['WindowCenter'],
                'seriesDescription': file_metadata['SeriesDescription']
            })
            # Отправляем прогресс обработки
            progress = (i + 1) / total_files * 100
            app.logger.info(f'Processing progress: {progress:.1f}%')
        
        app.logger.info('Successfully processed all files')
        return jsonify({
            'image': img_str,
            'metadata': metadata,
            'files': files_info,
            'currentIndex': 0,
            'uploadId': upload_id
        })
    except Exception as e:
        app.logger.error(f"Error processing folder: {str(e)}", exc_info=True)
        return jsonify({'error': f'Error processing folder: {str(e)}'}), 500

@app.route('/load_image', methods=['POST'])
def load_image():
    try:
        data = request.json
        if not data or 'filePath' not in data:
            return jsonify({'error': 'No file path provided'}), 400
            
        file_path = data['filePath']
        if not os.path.exists(file_path):
            return jsonify({'error': f'File not found: {file_path}'}), 404
            
        window_center = float(data.get('windowCenter', 127))
        window_width = float(data.get('windowWidth', 256))
        
        app.logger.info(f'Loading image from: {file_path}')
        img_str, metadata = process_dicom_file(file_path)
        
        app.logger.info('Image processed successfully')
        return jsonify({
            'image': img_str,
            'metadata': metadata
        })
    except Exception as e:
        app.logger.error(f"Error loading image: {str(e)}", exc_info=True)
        return jsonify({'error': f'Error loading image: {str(e)}'}), 500

@app.route('/adjust', methods=['POST'])
def adjust_image():
    try:
        data = request.json
        file_path = data['filePath']
        window_center = float(data['windowCenter'])
        window_width = float(data['windowWidth'])
        
        # Читаем DICOM файл
        dataset = pydicom.dcmread(file_path)
        pixel_array = dataset.pixel_array
        
        # Применяем window/level
        min_value = window_center - window_width / 2
        max_value = window_center + window_width / 2
        
        # Нормализуем значения в диапазон 0-255 с учетом window/level
        normalized_array = np.clip(pixel_array, min_value, max_value)
        normalized_array = ((normalized_array - min_value) / (max_value - min_value) * 255.0)
        normalized_array = np.clip(normalized_array, 0, 255).astype(np.uint8)
        
        # Конвертируем в изображение и сохраняем в base64
        image = Image.fromarray(normalized_array)
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return jsonify({'image': image_base64})
        
    except Exception as e:
        print(f"Error adjusting image: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/process_file', methods=['POST'])
def process_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
            
        file = request.files['file']
        file_data = file.read()
        
        image_base64, metadata = process_dicom_file(file_data)
        
        return jsonify({
            'image': image_base64,
            'metadata': metadata
        })
        
    except Exception as e:
        print(f"Error processing file: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Добавляем маршрут для обслуживания статических файлов из папки assets с правильным MIME-типом
@app.route('/assets/<path:filename>')
def serve_assets(filename):
    response = send_from_directory('assets', filename)
    if filename.endswith('.js'):
        response.headers['Content-Type'] = 'application/javascript'
    return response

if __name__ == '__main__':
    app.run(debug=True) 