# Запуск Почты
## На компьютере
Скачайте START_CONTEXT.py и откройте через Python 3.11 или новее. Он открывает браузер и выбирает свободный порт. Оставьте консоль открытой, пока пользуетесь почтой.

```sh
python START_CONTEXT.py --demo
```

## На сервере
Из корня клонированного репозитория выполните команды ниже. Образ содержит интерфейс, сервер, движок и 30 демонстрационных писем. После запуска откройте http://localhost:8765 на сервере.

```sh
docker build -t context-mail .
docker run --rm --name context-mail -p 127.0.0.1:8765:8765 context-mail
```

Для собственного домена направьте HTTPS reverse proxy на 127.0.0.1:8765. Интерфейс позволяет просматривать все загруженные ящики. Общий демосервис предназначен для демонстрационных данных.

## Полный корпус организаторов
Поместите пять исходных файлов организаторов в папку data. Перечень включает documents.jsonl, queries_dev.jsonl, qrels_dev.jsonl, README.md и manifest.json. Загрузка корпуса проверяет SHA-256. В Linux команда монтирует папку только для чтения.

```sh
docker run --rm -p 127.0.0.1:8765:8765 -v "$PWD/data:/app/data:ro" context-mail --dataset /app/data
```

Для запуска без Docker достаточно команды `python START_CONTEXT.py --dataset data`. У файла запуска есть также режим `--no-browser --port 8765`. В демонстрационном наборе выберите «Поездки» и введите «Где писали, что самолёт улетит позже?».

## Для проверяющих
```sh
python -m unittest discover -s tests -v
python -m mailsearch.evaluate --dataset data --mode smart --k 1 5 10 --export-k 10 --output submission.jsonl
python -m mailsearch.loadtest --queries data/queries_dev.jsonl --workers 4 --repeat 3
```

Последняя команда обращается к уже запущенному серверу с тем же корпусом. Для локального редактирования используйте папки mailsearch и web. После правок выполните `python build_standalone.py` для обновления однофайловой версии.
