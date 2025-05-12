// Импортируем локальную версию библиотеки для работы с DICOM
importScripts('../assets/dicom-parser.min.js');

self.onmessage = function(e) {
    const { file, windowWidth, windowCenter } = e.data;
    
    const reader = new FileReader();
    reader.onload = function(event) {
        try {
            // Преобразуем ArrayBuffer в Uint8Array для корректной работы парсера
            const byteArray = new Uint8Array(event.target.result);
            
            // Парсим DICOM файл
            const dataSet = dicomParser.parseDicom(byteArray);
            
            // Извлекаем метаданные
            const metadata = {
                'PatientName': dataSet.string('x00100010') || '',
                'PatientID': dataSet.string('x00100020') || '',
                'StudyDate': dataSet.string('x00080020') || '',
                'Modality': dataSet.string('x00080060') || '',
                'SeriesDescription': dataSet.string('x0008103e') || '',
                'WindowWidth': parseFloat(dataSet.string('x00281051')) || 256,
                'WindowCenter': parseFloat(dataSet.string('x00281050')) || 127
            };
            
            // Получаем пиксельные данные
            const pixelDataElement = dataSet.elements.x7fe00010;
            if (!pixelDataElement) {
                throw new Error('No pixel data found in DICOM file');
            }

            const rows = dataSet.uint16('x00280010');
            const columns = dataSet.uint16('x00280011');
            const bitsAllocated = dataSet.uint16('x00280100');
            const bitsStored = dataSet.uint16('x00280101') || bitsAllocated;
            const highBit = dataSet.uint16('x00280102') || (bitsStored - 1);
            const pixelRepresentation = dataSet.uint16('x00280103') || 0;
            const samplesPerPixel = dataSet.uint16('x00280002') || 1;
            const photometricInterpretation = dataSet.string('x00280004') || 'MONOCHROME2';
            
            // Получаем пиксельные данные в правильном формате
            const pixelDataOffset = pixelDataElement.dataOffset;
            const pixelDataLength = pixelDataElement.length;
            
            let pixelData;
            if (bitsAllocated === 16) {
                // Создаем новый ArrayBuffer для копирования данных
                const pixelBuffer = new ArrayBuffer(pixelDataLength);
                const pixelBufferView = new Uint8Array(pixelBuffer);
                pixelBufferView.set(byteArray.slice(pixelDataOffset, pixelDataOffset + pixelDataLength));
                
                pixelData = pixelRepresentation === 1 ? 
                    new Int16Array(pixelBuffer) :
                    new Uint16Array(pixelBuffer);
            } else {
                pixelData = new Uint8Array(
                    byteArray.slice(pixelDataOffset, pixelDataOffset + pixelDataLength).buffer
                );
            }
            
            // Находим минимальное и максимальное значения
            let min = Infinity;
            let max = -Infinity;
            for (let i = 0; i < pixelData.length; i++) {
                const value = pixelData[i];
                if (value < min) min = value;
                if (value > max) max = value;
            }
            
            // Создаем canvas для обработки изображения
            const canvas = new OffscreenCanvas(columns, rows);
            const ctx = canvas.getContext('2d');
            const imageData = ctx.createImageData(columns, rows);
            
            // Применяем настройки окна
            const ww = windowWidth || metadata.WindowWidth;
            const wc = windowCenter || metadata.WindowCenter;
            const minValue = wc - ww / 2;
            const maxValue = wc + ww / 2;
            
            // Заполняем ImageData
            for (let i = 0; i < rows * columns; i++) {
                let pixelValue = pixelData[i];
                
                // Применяем bit shifting если необходимо
                if (bitsStored < bitsAllocated) {
                    pixelValue = pixelValue >> (highBit - bitsStored + 1);
                }
                
                // Инвертируем значения для MONOCHROME1
                if (photometricInterpretation === 'MONOCHROME1') {
                    pixelValue = max - pixelValue;
                }
                
                // Применяем window/level
                let windowedValue;
                if (pixelValue <= minValue) {
                    windowedValue = 0;
                } else if (pixelValue > maxValue) {
                    windowedValue = 255;
                } else {
                    windowedValue = ((pixelValue - minValue) / (ww)) * 255;
                }
                
                // Заполняем RGBA каналы
                const pixelIndex = i * 4;
                imageData.data[pixelIndex] = windowedValue;     // R
                imageData.data[pixelIndex + 1] = windowedValue; // G
                imageData.data[pixelIndex + 2] = windowedValue; // B
                imageData.data[pixelIndex + 3] = 255;          // A
            }
            
            // Рисуем изображение на canvas
            ctx.putImageData(imageData, 0, 0);
            
            // Конвертируем в base64
            canvas.convertToBlob({ type: 'image/png' }).then(blob => {
                const reader = new FileReader();
                reader.onload = function() {
                    const base64Image = reader.result.split(',')[1];
                    self.postMessage({
                        image: base64Image,
                        metadata: metadata
                    });
                };
                reader.onerror = function(error) {
                    console.error('Error converting blob to base64:', error);
                    self.postMessage({ error: 'Error converting image to base64' });
                };
                reader.readAsDataURL(blob);
            }).catch(error => {
                console.error('Error converting canvas to blob:', error);
                self.postMessage({ error: 'Error converting canvas to blob' });
            });
            
        } catch (error) {
            console.error('Error processing DICOM:', error);
            self.postMessage({ error: error.message });
        }
    };
    
    reader.onerror = function(error) {
        console.error('Error reading file:', error);
        self.postMessage({ error: 'Error reading DICOM file' });
    };
    
    reader.readAsArrayBuffer(file);
}; 