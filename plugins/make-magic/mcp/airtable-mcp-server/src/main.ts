#!/usr/bin/env node

import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {AirtableService} from './airtableService.js';
import {createServer} from './index.js';

function setupSignalHandlers(cleanup: () => Promise<void>): void {
	process.on('SIGINT', async () => {
		await cleanup();
		process.exit(0);
	});
	process.on('SIGTERM', async () => {
		await cleanup();
		process.exit(0);
	});
}

(async () => {
	const apiKey = process.argv.slice(2)[0];
	if (apiKey) {
		console.warn('warning (airtable-mcp-server): Passing in an API key as a command-line argument is deprecated and may be removed in a future version. Instead, set the `AIRTABLE_API_KEY` environment variable. See https://github.com/domdomegg/airtable-mcp-server/blob/master/README.md#usage for an example with Claude Desktop.');
	}

	const airtableService = new AirtableService(apiKey);

	// This fork ships stdio-only. The upstream HTTP (StreamableHTTPServerTransport/Express) transport
	// has been removed — see PATCHES.md.
	const server = createServer({airtableService});
	setupSignalHandlers(async () => server.close());

	const stdioTransport = new StdioServerTransport();
	await server.connect(stdioTransport);
})().catch((error: unknown) => {
	console.error(error);
	process.exit(1);
});
