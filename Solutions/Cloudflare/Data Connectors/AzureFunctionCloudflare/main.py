import os
import asyncio
from azure.storage.blob.aio import ContainerClient
import json
import logging
import azure.functions as func
import re
import time
import aiohttp
from typing import AsyncIterable, Optional
from io import BytesIO
import gzip

from .sentinel_connector_async import AzureSentinelConnectorAsync


logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.ERROR)


MAX_SCRIPT_EXEC_TIME_MINUTES = 5


AZURE_STORAGE_CONNECTION_STRING = os.environ['AZURE_STORAGE_CONNECTION_STRING']
CONTAINER_NAME = os.environ['CONTAINER_NAME']
WORKSPACE_ID = os.environ['WORKSPACE_ID']
SHARED_KEY = os.environ['SHARED_KEY']
LOG_TYPE = 'Cloudflare'
LINE_SEPARATOR = os.environ.get('lineSeparator',  '[\n\r\x0b\v\x0c\f\x1c\x1d\x85\x1e\u2028\u2029]+(?=\{)')
    
# Defines how many files can be processed simultaneously
MAX_CONCURRENT_PROCESSING_FILES = int(os.environ.get('MAX_CONCURRENT_PROCESSING_FILES', 20))

# Defines page size while listing files from blob storage. New page is not processed while old page is processing.
MAX_PAGE_SIZE = int(MAX_CONCURRENT_PROCESSING_FILES * 1.5)

# Defines max number of events that can be sent in one request to Azure Sentinel
MAX_BUCKET_SIZE = int(os.environ.get('MAX_BUCKET_SIZE', 2000))

LOG_ANALYTICS_URI = os.environ.get('logAnalyticsUri')

if not LOG_ANALYTICS_URI or str(LOG_ANALYTICS_URI).isspace():
    LOG_ANALYTICS_URI = 'https://' + WORKSPACE_ID + '.ods.opinsights.azure.com'

pattern = r'https:\/\/([\w\-]+)\.ods\.opinsights\.azure.([a-zA-Z\.]+)$'
match = re.match(pattern, str(LOG_ANALYTICS_URI))
if not match:
    raise Exception("Invalid Log Analytics Uri.")


async def main(mytimer: func.TimerRequest):
    logging.info('Starting script')
    logging.info('Concurrency parameters: MAX_CONCURRENT_PROCESSING_FILES {}, MAX_PAGE_SIZE {}, MAX_BUCKET_SIZE {}.'.format(MAX_CONCURRENT_PROCESSING_FILES, MAX_PAGE_SIZE, MAX_BUCKET_SIZE))
    conn = AzureBlobStorageConnector(AZURE_STORAGE_CONNECTION_STRING, CONTAINER_NAME, MAX_CONCURRENT_PROCESSING_FILES)
    container_client = conn._create_container_client()
    async with container_client:
        async with aiohttp.ClientSession() as session:
            cors = []
            async for blob in conn.get_blobs():
                cor = conn.process_blob(blob, container_client, session)
                cors.append(cor)
                if len(cors) >= MAX_PAGE_SIZE:
                    await asyncio.gather(*cors)
                    cors = []
                if conn.check_if_script_runs_too_long():
                    logging.info('Script is running too long. Stop processing new blobs.')
                    break

            if cors:
                await asyncio.gather(*cors)
                logging.info('Processed {} files with {} events.'.format(conn.total_blobs, conn.total_events))

    logging.info('Script finished. Processed files: {}. Processed events: {}'.format(conn.total_blobs, conn.total_events))


class AzureBlobStorageConnector:
    def __init__(self, conn_string, container_name, max_concurrent_processing_fiiles=10):
        self.__conn_string = conn_string
        self.__container_name = container_name
        self.semaphore = asyncio.Semaphore(max_concurrent_processing_fiiles)
        self.script_start_time = int(time.time())
        self.total_blobs = 0
        self.total_events = 0

    def _create_container_client(self):
        return ContainerClient.from_connection_string(self.__conn_string, self.__container_name, logging_enable=False, max_single_get_size=2*1024*1024, max_chunk_get_size=2*1024*1024)

    async def get_blobs(self):
        container_client = self._create_container_client()
        async with container_client:
            async for blob in container_client.list_blobs():
                if 'ownership-challenge' not in blob['name']:
                    yield blob

    def check_if_script_runs_too_long(self):
        now = int(time.time())
        duration = now - self.script_start_time
        max_duration = int(MAX_SCRIPT_EXEC_TIME_MINUTES * 60 * 0.85)
        return duration > max_duration

    async def delete_blob(self, blob, container_client):
        logging.info("Deleting blob {}".format(blob['name']))
        await container_client.delete_blob(blob['name'])

    async def process_blob(self, blob, container_client, session: aiohttp.ClientSession):
        async with self.semaphore:
            logging.info("Start processing {}".format(blob['name']))
            sentinel = AzureSentinelConnectorAsync(session, LOG_ANALYTICS_URI, WORKSPACE_ID, SHARED_KEY, LOG_TYPE, queue_size=MAX_BUCKET_SIZE)
            s = ''
            async for chunk in self._get_chunks(blob, container_client):
                s += chunk
                lines = re.split(r'{0}'.format(LINE_SEPARATOR), s)
                for n, line in enumerate(lines):
                    if n < len(lines) - 1:
                        if line:
                            try :
                                event = json.loads(line)
                            except ValueError as e:
                                logging.error('Error while loading json Event at line value {}. blob name: {}. Error: {}'.format(line, blob['name'],str(e)))
                                raise e
                            await sentinel.send(event)
                s = line
            if s:
                try:
                    event = json.loads(s)
                except ValueError as e:
                    logging.error('Error while loading json Event at s value {}. blob name: {}. Error: {}'.format(line, blob['name'],str(e)))
                    raise e
                await sentinel.send(event)
            await sentinel.flush()
            await self.delete_blob(blob, container_client)
            self.total_blobs += 1
            self.total_events += sentinel.successfull_sent_events_number
            logging.info("Finish processing {}. Sent events: {}".format(blob['name'], sentinel.successfull_sent_events_number))
            if self.total_blobs % 100 == 0:
                logging.info('Processed {} files with {} events.'.format(self.total_blobs, self.total_events))

    @classmethod
    async def _get_chunks(cls, blob, container_client: ContainerClient) -> AsyncIterable[str]:
        blob.content_settings.content_encoding = None
        await container_client.get_blob_client(blob).set_http_headers(blob.content_settings)
        blob_cor = await container_client.download_blob(blob['name'])
        is_gz = None
        full_gz_file = b''
        async for chunk in blob_cor.chunks():
            if is_gz is None:
                if cls._check_if_gzipped(chunk):
                    is_gz = True
                else:
                    is_gz = False
            
            if is_gz is False:
                yield chunk.decode()
            elif is_gz is True:
                full_gz_file += chunk

        if is_gz and full_gz_file:
            full_gz_file = BytesIO(full_gz_file)
            f = gzip.GzipFile(fileobj=full_gz_file)
            while True:
                line = f.readline()
                if line:
                    yield line.decode()
                else:
                    break

    @staticmethod
    def _check_if_gzipped(b: bytes) -> Optional[bool]:
        if b:
            return b[:2] == b'\x1f\x8b'
