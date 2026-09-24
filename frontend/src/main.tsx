import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";

import { createTheme, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./app/App";
import { PersonaProvider } from "./app/persona";

const theme = createTheme({
  primaryColor: "teal",
  fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
});

function Providers() {
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } }),
  );
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <PersonaProvider>
            <Notifications />
            <App />
          </PersonaProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
}

const root = document.getElementById("root");
if (root === null) throw new Error("InvoiceOps root element is missing");
createRoot(root).render(<Providers />);
